"""WorkVault -> Vault API sync client (Phase 1: one-way, PC -> server).

    python sync.py              full sync, then watch for changes (Ctrl+C stops)
    python sync.py --once       one full sync, then exit
    python sync.py --dry-run    list what would be sent or skipped; sends nothing
    python sync.py --check      verify VAULT_API_URL and VAULT_API_KEY

Only metadata (paths, counts, reasons) is logged. Note bodies and the API key
never are. Deleting or renaming a note does not remove it from the server:
Phase 1 has no delete API.
"""
import argparse
import hashlib
import json
import logging
import logging.handlers
import os
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone

import requests

import filters
from config import ConfigError, load_config
from watcher import RESCAN, Debouncer, start_watching

logger = logging.getLogger("vault_sync")

STATE_VERSION = 1
# Bump when the exclusion or secret rules change so every note is re-evaluated.
RULES_VERSION = 1
USER_AGENT = "obsidian-vault-bridge-client/1.0"


@dataclass
class NoteFile:
    rel_path: str
    content: str
    content_hash: str
    size: int
    mtime_ns: int
    created_at: str
    modified_at: str

    def payload(self):
        data = {"path": self.rel_path, "content": self.content, "content_hash": self.content_hash}
        if self.created_at:
            data["created_at"] = self.created_at
        if self.modified_at:
            data["modified_at"] = self.modified_at
        return data


@dataclass
class Decision:
    # send | touched (same content, new mtime) | unchanged | skip | excluded | gone | retry
    action: str
    note: NoteFile = None
    reason: str = ""
    content_hash: str = None
    stat: os.stat_result = None


class SkipFile(Exception):
    def __init__(self, reason, content_hash=None):
        super().__init__(reason)
        self.reason = reason
        self.content_hash = content_hash


class ApiError(Exception):
    pass


# --------------------------------------------------------------------------
# Scanning and reading


def scan_vault(config):
    """All .md files under folders that pass the folder rules.

    Returns (note_paths, excluded_folders) with vault-relative "/" paths.
    """
    root = config.vault_root
    notes, excluded = [], []
    for dirpath, dirnames, filenames in os.walk(root):  # never follows links
        rel_dir = os.path.relpath(dirpath, root).replace(os.sep, "/")
        rel_dir = "" if rel_dir == "." else rel_dir
        kept = []
        for name in sorted(dirnames):
            reason = filters.name_exclusion_reason(name) or filters.fs_exclusion_reason(os.path.join(dirpath, name))
            if reason:
                excluded.append((f"{rel_dir}/{name}" if rel_dir else name, reason))
            else:
                kept.append(name)
        dirnames[:] = kept
        for name in sorted(filenames):
            if name.lower().endswith(filters.NOTE_SUFFIX):
                notes.append(f"{rel_dir}/{name}" if rel_dir else name)
    return notes, excluded


def exclusion_reason(config, rel):
    """Path rules, then filesystem rules for the file and each parent folder."""
    reason = filters.path_exclusion_reason(rel, config.extra_excludes)
    if reason:
        return reason
    current = config.vault_root
    for part in rel.split("/"):
        current = current / part
        reason = filters.fs_exclusion_reason(current)
        if reason:
            return reason
    try:
        (config.vault_root / rel).resolve().relative_to(config.vault_root)
    except ValueError:
        return "outside_vault"
    return None


def _iso(timestamp):
    try:
        return datetime.fromtimestamp(timestamp, tz=timezone.utc).isoformat()
    except (OverflowError, OSError, ValueError):
        return ""


def read_note(config, rel, st):
    if st.st_size > config.max_note_bytes:
        raise SkipFile("too_large")
    raw = (config.vault_root / rel).read_bytes()
    if len(raw) > config.max_note_bytes:
        raise SkipFile("too_large")
    digest = hashlib.sha256(raw).hexdigest()
    try:
        # Strict UTF-8 round-trips byte for byte, so the server's hash of the
        # content equals this hash of the file.
        content = raw.decode("utf-8")
    except UnicodeDecodeError:
        raise SkipFile("not_utf8", digest) from None
    if "\x00" in content:
        raise SkipFile("binary_content", digest)
    created = getattr(st, "st_birthtime", st.st_ctime)  # st_ctime is creation time on Windows
    return NoteFile(
        rel_path=rel,
        content=content,
        content_hash=digest,
        size=len(raw),
        mtime_ns=st.st_mtime_ns,
        created_at=_iso(created),
        modified_at=_iso(st.st_mtime),
    )


# --------------------------------------------------------------------------
# Local state


class SyncState:
    """Per note: hash, mtime and size of the last synced (or deliberately skipped) version.

    Lives outside the vault (default %LOCALAPPDATA%\\obsidian-vault-bridge) and
    resets itself when the server, vault or filtering rules change.
    """

    def __init__(self, config):
        self.path = config.state_file
        self.identity = {
            "api_url": config.api_url,
            "vault_root": str(config.vault_root),
            "rules": {
                "version": RULES_VERSION,
                "extra_excludes": list(config.extra_excludes),
                "secret_scan": config.secret_scan,
                "secret_scan_allow": list(config.secret_scan_allow),
                "max_note_bytes": config.max_note_bytes,
            },
        }
        self.notes = {}
        self._dirty = False
        self._load()

    def _load(self):
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return
        except (OSError, ValueError):
            logger.warning("State file %s is unreadable; starting fresh", self.path)
            return
        if data.get("version") != STATE_VERSION or data.get("identity") != self.identity:
            logger.info("Server, vault or rules changed; re-evaluating every note")
            self._dirty = True
            return
        self.notes = data.get("notes", {})

    def get(self, rel):
        return self.notes.get(rel)

    def record(self, rel, *, content_hash, mtime_ns, size, skipped=None):
        self.notes[rel] = {"hash": content_hash, "mtime_ns": mtime_ns, "size": size, "skipped": skipped}
        self._dirty = True

    def save(self):
        if not self._dirty:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(self.path.name + ".tmp")
        payload = {"version": STATE_VERSION, "identity": self.identity, "notes": self.notes}
        tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, self.path)
        self._dirty = False


# --------------------------------------------------------------------------
# HTTP


class VaultApiClient:
    RETRY_STATUSES = {429, 500, 502, 503, 504}

    def __init__(self, config, session=None, sleep=time.sleep, attempts=4):
        self._config = config
        self._sleep = sleep
        self._attempts = attempts
        self._session = session or requests.Session()
        self._session.headers.update(
            {
                "Authorization": f"Bearer {config.api_key}",
                "User-Agent": USER_AGENT,
                "Accept": "application/json",
            }
        )
        self._session.verify = config.ca_bundle or True

    def push(self, notes):
        body = json.dumps({"notes": notes}, ensure_ascii=False).encode("utf-8")
        data = self._request(
            "POST", "sync", data=body, headers={"Content-Type": "application/json; charset=utf-8"}
        )
        results = data.get("results")
        if not isinstance(results, list) or len(results) != len(notes):
            raise ApiError("unexpected response from server")
        return results

    def check(self):
        return self._request("GET", "recent", params={"limit": 1})

    def _request(self, method, name, **kwargs):
        url = self._config.endpoint(name)
        delay = 2.0
        for attempt in range(1, self._attempts + 1):
            try:
                # Redirects are refused: they usually mean a wrong URL and
                # would turn the POST into a GET.
                response = self._session.request(
                    method, url, timeout=self._config.request_timeout, allow_redirects=False, **kwargs
                )
            except requests.RequestException as exc:
                problem = type(exc).__name__
                wait = delay
            else:
                if response.status_code == 200:
                    try:
                        return response.json()
                    except ValueError:
                        raise ApiError("server returned invalid JSON") from None
                if response.status_code not in self.RETRY_STATUSES:
                    raise ApiError(_describe(response))
                problem = f"HTTP {response.status_code}"
                retry_after = response.headers.get("Retry-After", "")
                wait = min(float(retry_after), 300.0) if retry_after.isdigit() else delay
            if attempt == self._attempts:
                raise ApiError(f"{problem} (gave up after {attempt} attempts)")
            logger.warning("Request failed (%s); retrying in %.0fs", problem, wait)
            self._sleep(wait)
            delay *= 2
        raise AssertionError("unreachable")


def _describe(response):
    if 300 <= response.status_code < 400:
        return f"HTTP {response.status_code} redirect; check VAULT_API_URL (https and full /api/vault path)"
    try:
        error = response.json().get("error", {})
        return f"HTTP {response.status_code} {error.get('code', '')}: {error.get('message', '')}".strip()
    except (ValueError, AttributeError):
        return f"HTTP {response.status_code}"


# --------------------------------------------------------------------------
# Sync


def make_batches(notes, max_notes, max_bytes):
    batch, size = [], 0
    for note in notes:
        note_bytes = len(json.dumps(note.payload(), ensure_ascii=False).encode("utf-8"))
        if batch and (len(batch) >= max_notes or size + note_bytes > max_bytes):
            yield batch
            batch, size = [], 0
        batch.append(note)
        size += note_bytes
    if batch:
        yield batch


class Syncer:
    def __init__(self, config, client, state):
        self.config = config
        self.client = client
        self.state = state

    def evaluate(self, rel, force=False):
        reason = exclusion_reason(self.config, rel)
        if reason == "missing":
            return Decision("gone")
        if reason:
            return Decision("excluded", reason=reason)
        try:
            st = os.stat(self.config.vault_root / rel)
        except FileNotFoundError:
            return Decision("gone")
        except OSError as exc:
            return Decision("retry", reason=type(exc).__name__)

        prev = self.state.get(rel)
        if not force and prev and prev["mtime_ns"] == st.st_mtime_ns and prev["size"] == st.st_size:
            return Decision("unchanged")
        try:
            note = read_note(self.config, rel, st)
        except SkipFile as skip:
            return Decision("skip", reason=skip.reason, content_hash=skip.content_hash, stat=st)
        except OSError as exc:  # e.g. locked while Obsidian is writing
            return Decision("retry", reason=type(exc).__name__)

        if self.config.secret_scan and not filters.matches_any(rel, self.config.secret_scan_allow):
            found = filters.find_secret(note.content)
            if found:
                return Decision("skip", reason=f"possible_secret:{found}", content_hash=note.content_hash, stat=st)
        if not force and prev and not prev.get("skipped") and prev["hash"] == note.content_hash:
            return Decision("touched", note=note)
        return Decision("send", note=note)

    def full_scan(self, force=False):
        paths, _ = scan_vault(self.config)
        logger.info("Full scan: %d notes in vault", len(paths))
        return self.sync_paths(paths, force=force)

    def sync_paths(self, rel_paths, force=False):
        """Sync the given notes. Returns False if something should be retried soon."""
        to_send, healthy = [], True
        for rel in sorted(set(rel_paths)):
            decision = self.evaluate(rel, force)
            if decision.action == "send":
                to_send.append(decision.note)
            elif decision.action == "touched":
                self._record_synced(decision.note)
            elif decision.action == "skip":
                self._record_skip(rel, decision)
            elif decision.action == "retry":
                logger.warning("Cannot read %s (%s); will retry", rel, decision.reason)
                healthy = False
            elif decision.action == "excluded":
                logger.debug("Excluded %s (%s)", rel, decision.reason)

        counts = {}
        for batch in make_batches(to_send, self.config.batch_max_notes, self.config.batch_max_bytes):
            try:
                results = self.client.push([note.payload() for note in batch])
            except ApiError as exc:
                logger.error("Sync of %d notes failed: %s", len(batch), exc)
                healthy = False
                break
            for note, result in zip(batch, results):
                status = self._apply_result(note, result)
                counts[status] = counts.get(status, 0) + 1
        if counts:
            logger.info("Pushed %d notes: %s", sum(counts.values()), ", ".join(f"{k}={v}" for k, v in sorted(counts.items())))
        self.state.save()
        return healthy

    def _apply_result(self, note, result):
        status = result.get("status") if isinstance(result, dict) else None
        if status in ("created", "updated", "unchanged"):
            self._record_synced(note)
            if status != "unchanged":
                logger.info("Synced %s (%s)", note.rel_path, status)
            return status
        error = (result.get("error") if isinstance(result, dict) else None) or "rejected"
        logger.warning("Server rejected %s (%s)", note.rel_path, error)
        if error != "conflict_retry":
            # Do not resend until the file changes.
            self.state.record(
                note.rel_path,
                content_hash=note.content_hash,
                mtime_ns=note.mtime_ns,
                size=note.size,
                skipped=f"server:{error}",
            )
        return "rejected"

    def _record_synced(self, note):
        self.state.record(note.rel_path, content_hash=note.content_hash, mtime_ns=note.mtime_ns, size=note.size)

    def _record_skip(self, rel, decision):
        prev = self.state.get(rel)
        if not prev or prev.get("skipped") != decision.reason or prev.get("hash") != decision.content_hash:
            logger.warning("Not synced: %s (%s)", rel, decision.reason)
        self.state.record(
            rel,
            content_hash=decision.content_hash,
            mtime_ns=decision.stat.st_mtime_ns,
            size=decision.stat.st_size,
            skipped=decision.reason,
        )


# --------------------------------------------------------------------------
# Modes


def run_forever(config, syncer, force=False):
    debouncer = Debouncer(config.debounce_seconds)
    # Start watching before the initial scan so edits made during it are not lost.
    observer = start_watching(config.vault_root, debouncer, config.extra_excludes)
    try:
        healthy = syncer.full_scan(force=force)
        next_scan = time.monotonic() + (config.rescan_interval_seconds if healthy else config.retry_interval_seconds)
        while True:
            time.sleep(0.5)
            ready = debouncer.pop_ready()
            try:
                if RESCAN in ready or time.monotonic() >= next_scan:
                    healthy = syncer.full_scan()
                    interval = config.rescan_interval_seconds if healthy else config.retry_interval_seconds
                    next_scan = time.monotonic() + interval
                elif ready and not syncer.sync_paths(ready):
                    next_scan = min(next_scan, time.monotonic() + config.retry_interval_seconds)
            except Exception:  # keep a background service alive
                logger.exception("Unexpected error; continuing")
    except KeyboardInterrupt:
        logger.info("Stopping")
    finally:
        observer.stop()
        observer.join(timeout=5)


def dry_run(config, out=sys.stdout):
    """Report what a sync would do, without sending anything or touching state."""
    syncer = Syncer(config, client=None, state=SyncState(config))
    paths, excluded_folders = scan_vault(config)
    groups = {"send": [], "unchanged": [], "skip": []}
    for rel in paths:
        decision = syncer.evaluate(rel)
        if decision.action in ("send",):
            groups["send"].append((rel, ""))
        elif decision.action in ("unchanged", "touched"):
            groups["unchanged"].append((rel, ""))
        elif decision.action in ("skip", "excluded", "retry"):
            groups["skip"].append((rel, decision.reason))

    print(f"Vault: {config.vault_root}", file=out)
    print(f"Excluded folders ({len(excluded_folders)}):", file=out)
    for rel, reason in excluded_folders:
        print(f"  - {rel}/  [{reason}]", file=out)
    print(f"Not synced ({len(groups['skip'])}):", file=out)
    for rel, reason in groups["skip"]:
        print(f"  - {rel}  [{reason}]", file=out)
    print(f"Would send ({len(groups['send'])}):", file=out)
    for rel, _ in groups["send"]:
        print(f"  + {rel}", file=out)
    print(f"Already synced ({len(groups['unchanged'])})", file=out)
    return 0


class SingleInstance:
    """Prevents two clients from writing the same state file."""

    def __init__(self, path):
        self.path = path
        self._handle = None

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle = open(self.path, "a+")
        try:
            if os.name == "nt":
                import msvcrt

                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            handle.close()
            raise ConfigError(f"Another sync client is already running (lock file {self.path})") from None
        self._handle = handle
        return self

    def __exit__(self, *exc_info):
        self._handle.close()  # releases the lock


def setup_logging(config, verbose=False):
    log = logging.getLogger("vault_sync")
    log.setLevel(logging.DEBUG if verbose else logging.INFO)
    formatter = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
    if sys.stderr is not None:  # None under pythonw.exe
        console = logging.StreamHandler()
        console.setFormatter(formatter)
        log.addHandler(console)
    try:
        config.log_file.parent.mkdir(parents=True, exist_ok=True)
        file_handler = logging.handlers.RotatingFileHandler(
            config.log_file, maxBytes=1_000_000, backupCount=3, encoding="utf-8"
        )
        file_handler.setFormatter(formatter)
        log.addHandler(file_handler)
    except OSError:
        log.warning("Cannot write log file %s", config.log_file)


def main(argv=None):
    parser = argparse.ArgumentParser(description="One-way sync of the Obsidian WorkVault to the Vault API.")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--once", action="store_true", help="run one full sync and exit")
    mode.add_argument("--dry-run", action="store_true", help="show what would be synced; sends nothing")
    mode.add_argument("--check", action="store_true", help="verify VAULT_API_URL and VAULT_API_KEY")
    parser.add_argument("--force", action="store_true", help="re-send notes even if unchanged since last sync")
    parser.add_argument("--env-file", help="path to the .env file (default: windows_client/.env)")
    parser.add_argument("-v", "--verbose", action="store_true", help="debug logging")
    args = parser.parse_args(argv)

    if sys.stdout is not None and hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(errors="replace")
    try:
        config = load_config(args.env_file, require_api=not args.dry_run)
    except ConfigError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 2

    if args.dry_run:
        return dry_run(config)

    setup_logging(config, args.verbose)
    client = VaultApiClient(config)
    if args.check:
        try:
            client.check()
        except ApiError as exc:
            logger.error("Check failed: %s", exc)
            return 1
        logger.info("OK: %s is reachable and the API key is accepted", config.api_url)
        return 0

    try:
        with SingleInstance(config.state_file.with_name(config.state_file.name + ".lock")):
            syncer = Syncer(config, client, SyncState(config))
            if args.once:
                return 0 if syncer.full_scan(force=args.force) else 1
            run_forever(config, syncer, force=args.force)
    except ConfigError as exc:
        logger.error("%s", exc)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
