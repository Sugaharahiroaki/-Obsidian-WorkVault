"""Phase 3: applies writes requested by AI clients to the real vault.

Changes are pulled from the Vault API and applied oldest first. Rules:
- only inside the writable folders; never hidden, excluded or linked paths
- nothing is ever deleted; existing files are replaced atomically
- create never overwrites: if the file exists, a conflict copy is written
- update only replaces a file that still has ``base_hash``; otherwise the new
  text goes to a conflict copy next to it
- append adds to whatever the file contains now (it never loses PC edits)
"""
import hashlib
import os
import uuid
from datetime import datetime
from pathlib import Path

import filters

WRITABLE_FOLDERS = (
    "00_INBOX",
    "01_Projects",
    "02_Ideas",
    "03_Knowledge",
    "04_AI-Chats",
    "05_Prompts",
    "06_Decisions",
    "07_DevLogs",
)
OPS = ("create", "append", "update")

APPLIED = "applied"
CONFLICT = "conflict"
FAILED = "failed"


def append_text(current, text):
    """Same rule as the server (vault_api/vault/services/writes.py), byte for byte."""
    newline = "\r\n" if "\r\n" in current else "\n"
    text = text.replace("\r\n", "\n").replace("\n", newline)
    if current and not current.endswith("\n"):
        current += newline
    if not text.endswith(newline):
        text += newline
    return current + text


def conflict_name(rel, when, attempt=1):
    stem = rel[: -len(filters.NOTE_SUFFIX)]
    suffix = "" if attempt == 1 else f" {attempt}"
    return f"{stem} (conflict {when:%Y-%m-%d %H%M%S}{suffix}){filters.NOTE_SUFFIX}"


def target_reason(root, rel):
    """Why ``rel`` may not be written, or None."""
    if not isinstance(rel, str) or "\\" in rel or ":" in rel:
        return "invalid_path"
    reason = filters.path_exclusion_reason(rel)
    if reason:
        return reason
    top, sep, _ = rel.partition("/")
    if not sep or top not in WRITABLE_FOLDERS:
        return "not_writable"
    current = Path(root)
    for part in rel.split("/"):
        current = current / part
        if os.path.lexists(current):
            reason = filters.fs_exclusion_reason(current)
            if reason:
                return reason
    try:
        (Path(root) / rel).resolve().relative_to(Path(root).resolve())
    except ValueError:
        return "outside_vault"
    return None


def apply_change(root, change, now=None):
    """Apply one change dict from GET /changes/. Returns the acknowledgement dict.

    Raises OSError for transient problems (e.g. file locked) so the caller can retry later.
    """
    root = Path(root)
    now = now or datetime.now()
    change_id = change.get("id")
    op, rel, content = change.get("op"), change.get("path"), change.get("content")
    if op not in OPS or not isinstance(content, str):
        return _ack(change_id, FAILED, detail="invalid_change")
    reason = target_reason(root, rel)
    if reason:
        return _ack(change_id, FAILED, detail=reason)

    target = root / rel
    if op == "create":
        if os.path.lexists(target):
            if _read_text(target) == content:
                return _ack(change_id, APPLIED, detail="already_present")
            return _conflict(root, change_id, rel, content, now, "exists_on_pc")
        _create(target, content)
        return _ack(change_id, APPLIED)

    if not os.path.lexists(target):
        return _conflict(root, change_id, rel, content, now, "missing_on_pc")
    raw = target.read_bytes()
    try:
        current = raw.decode("utf-8")
    except UnicodeDecodeError:
        return _ack(change_id, FAILED, detail="not_utf8")

    if op == "update":
        if raw == content.encode("utf-8"):
            return _ack(change_id, APPLIED, detail="already_present")
        if hashlib.sha256(raw).hexdigest() != (change.get("base_hash") or "").lower():
            return _conflict(root, change_id, rel, content, now, "changed_on_pc")
        _replace(target, content)
    else:  # append
        _replace(target, append_text(current, content))
    return _ack(change_id, APPLIED)


def _ack(change_id, status, conflict_path=None, detail=""):
    result = {"id": change_id, "status": status, "detail": detail}
    if conflict_path:
        result["conflict_path"] = conflict_path
    return result


def _conflict(root, change_id, rel, content, now, detail):
    for attempt in range(1, 100):
        candidate = conflict_name(rel, now, attempt)
        try:
            _create(root / candidate, content)
        except FileExistsError:
            continue
        return _ack(change_id, CONFLICT, conflict_path=candidate, detail=detail)
    return _ack(change_id, FAILED, detail="no_free_conflict_name")


def _read_text(path):
    try:
        return path.read_bytes().decode("utf-8")
    except (OSError, UnicodeDecodeError):
        return None


def _create(target, content):
    """Exclusive create: raises FileExistsError instead of overwriting."""
    target.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0))
    with os.fdopen(fd, "wb") as handle:
        handle.write(content.encode("utf-8"))


def _replace(target, content):
    # The temp name starts with "." so the watcher and the sync rules ignore it.
    tmp = target.with_name(f".{target.name}.{uuid.uuid4().hex[:8]}.tmp")
    tmp.write_bytes(content.encode("utf-8"))
    try:
        os.replace(tmp, target)
    except OSError:
        tmp.unlink(missing_ok=True)
        raise
