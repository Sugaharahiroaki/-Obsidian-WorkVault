"""Validation of vault-relative paths.

Every path coming from a client goes through ``normalize_note_path`` or
``normalize_folder_path`` before it touches the database (and, from Phase 3,
the filesystem). Only plain relative paths inside the vault are accepted;
traversal is rejected in every encoding we know of.
"""
import fnmatch
import posixpath
import re
import unicodedata
from urllib.parse import unquote

MAX_PATH_CHARS = 1024
MAX_SEGMENT_CHARS = 255
NOTE_SUFFIX = ".md"

# Never stored or served even if a client sends them. Mirrors the sync
# client's rules in windows_client/filters.py. Matched case-insensitively
# against every path segment; hidden names (".obsidian", ".env", ...) are
# rejected separately.
EXCLUDED_NAME_PATTERNS = ("*.key", "*.pem", "*.crt", "*.pfx", "secrets*", "password*")

_CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f]")
# Characters Windows does not allow in file names (":" also covers drive
# letters and NTFS alternate data streams).
_WINDOWS_FORBIDDEN = re.compile(r'[<>:"|?*]')
_WINDOWS_RESERVED = re.compile(r"^(con|prn|aux|nul|com[0-9]|lpt[0-9])(\..*)?$", re.IGNORECASE)
_MAX_DECODE_ROUNDS = 5


class InvalidPath(ValueError):
    """The value is not a safe vault-relative path."""


def normalize_note_path(raw):
    """Return the canonical form of a note path such as ``01_Projects/A/project.md``.

    Raises InvalidPath for anything that is not a relative ``.md`` path inside
    the vault.
    """
    path = _normalize(raw, allow_empty=False)
    if not path.lower().endswith(NOTE_SUFFIX) or len(path.rsplit("/", 1)[-1]) <= len(NOTE_SUFFIX):
        raise InvalidPath("only .md notes are accessible")
    return path


def normalize_folder_path(raw):
    """Return the canonical form of a folder path; ``""`` means the vault root."""
    if raw is None:
        return ""
    if not isinstance(raw, str):
        raise InvalidPath("path must be a string")
    return _normalize(raw.strip().rstrip("/\\"), allow_empty=True)


def _normalize(raw, allow_empty):
    if not isinstance(raw, str):
        raise InvalidPath("path must be a string")
    if raw == "" and allow_empty:
        return ""
    if len(raw) > MAX_PATH_CHARS:
        raise InvalidPath("path is too long")

    candidate = _canonical(raw)
    _check(candidate)

    # Validate every URL-decoding level too, so "%2e%2e%2f" and
    # double-encoded "%252e%252e%252f" are caught even though only the
    # outer form is used as the key.
    decoded = raw
    for _ in range(_MAX_DECODE_ROUNDS):
        next_decoded = unquote(decoded)
        if next_decoded == decoded:
            break
        decoded = next_decoded
        _check(_canonical(decoded))
    else:
        raise InvalidPath("too many levels of URL encoding")

    return candidate


def _canonical(value):
    # Windows separators become "/", and NFC unifies macOS/iOS (NFD) names.
    return unicodedata.normalize("NFC", value.replace("\\", "/"))


def _check(path):
    if path == "":
        raise InvalidPath("path is empty")
    if _CONTROL_CHARS.search(path):
        raise InvalidPath("path contains control characters")
    if path.startswith("/"):
        raise InvalidPath("absolute paths are not allowed")
    if _WINDOWS_FORBIDDEN.search(path):
        raise InvalidPath("path contains forbidden characters")

    for segment in path.split("/"):
        _check_segment(segment)

    # Belt and braces: the path must stay inside the vault root.
    root = "/vault"
    resolved = posixpath.normpath(posixpath.join(root, path))
    if not resolved.startswith(root + "/"):
        raise InvalidPath("path escapes the vault")


def _check_segment(segment):
    if segment == "":
        raise InvalidPath("path contains an empty segment")
    if len(segment) > MAX_SEGMENT_CHARS:
        raise InvalidPath("path segment is too long")
    # Full-width dots etc. collapse to "." under NFKC.
    folded = unicodedata.normalize("NFKC", segment)
    if segment in (".", "..") or folded in (".", ".."):
        raise InvalidPath("relative segments are not allowed")
    if segment.startswith(".") or folded.startswith("."):
        raise InvalidPath("hidden files and folders are not accessible")
    if segment != segment.rstrip(". "):
        # Windows silently strips trailing dots/spaces, making aliases.
        raise InvalidPath("segments may not end with a dot or space")
    if _WINDOWS_RESERVED.match(segment):
        raise InvalidPath("reserved device name")
    lowered = segment.lower()
    for pattern in EXCLUDED_NAME_PATTERNS:
        if fnmatch.fnmatchcase(lowered, pattern):
            raise InvalidPath("path matches an excluded pattern")


# Phase 3: the only top-level folders AI clients may write into. 99_Archive and
# 08_Templates stay read-only, and files directly under the vault root
# (00_INBOX.md, AGENTS.md, ...) are never writable.
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

# Characters that Windows forbids in file names or that break Obsidian links.
_TITLE_UNSAFE = re.compile(r'[\\/<>:"|?*#^\[\]\x00-\x1f\x7f]')
MAX_TITLE_CHARS = 120


def ensure_writable(path):
    """``path`` must already be normalized. Raises InvalidPath outside WRITABLE_FOLDERS."""
    top, sep, _ = path.partition("/")
    if not sep or top not in WRITABLE_FOLDERS:
        raise InvalidPath("writing is only allowed inside: " + ", ".join(WRITABLE_FOLDERS))
    return path


def note_path_from_title(folder, title):
    """``("04_AI-Chats", "2026-10-07 会議: 要約?")`` -> ``"04_AI-Chats/2026-10-07 会議_ 要約_.md"``."""
    if not isinstance(title, str):
        raise InvalidPath("title must be a string")
    name = " ".join(_TITLE_UNSAFE.sub("_", title).split()).strip(". ")[:MAX_TITLE_CHARS].strip(". ")
    if not name:
        raise InvalidPath("title is empty")
    if not name.lower().endswith(NOTE_SUFFIX):
        name += NOTE_SUFFIX
    folder = normalize_folder_path(folder)
    return normalize_note_path(f"{folder}/{name}" if folder else name)


def conflict_path_for(path, when, attempt=1):
    """``"a/b.md"`` -> ``"a/b (conflict 2026-10-07 132000).md"`` (``" 2"`` etc. appended on retries)."""
    stem = path[: -len(NOTE_SUFFIX)]
    suffix = "" if attempt == 1 else f" {attempt}"
    return f"{stem} (conflict {when:%Y-%m-%d %H%M%S}{suffix}){NOTE_SUFFIX}"


def folder_of(path):
    """``"01_Projects/A/project.md"`` -> ``"01_Projects/A"``; root notes -> ``""``."""
    return path.rsplit("/", 1)[0] if "/" in path else ""


def title_of(path):
    """Obsidian shows the file name without extension as the note title."""
    name = path.rsplit("/", 1)[-1]
    return name[: -len(NOTE_SUFFIX)] if name.lower().endswith(NOTE_SUFFIX) else name
