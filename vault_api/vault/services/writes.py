"""Phase 3 writes from AI clients: create_note, append_note, update_note.

The server copy is updated immediately (so the AI can read its own writes) and
a PendingChange is queued for the Windows client, which applies it to the real
vault. Nothing is ever deleted, and an update never silently replaces a newer
version: if the note changed since the AI read it, the proposed text is saved
as a separate "(conflict ...)" note instead.
"""
import hashlib
import re

from django.conf import settings
from django.db import transaction
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from ..models import PendingChange, VaultNote
from ..security import (
    InvalidPath,
    conflict_path_for,
    ensure_writable,
    folder_of,
    normalize_note_path,
    note_path_from_title,
    title_of,
)
from .notes import NoteNotFound

QUEUED = "queued"
CONFLICT = "conflict"

_HASH = re.compile(r"^[0-9a-fA-F]{64}$")
_FRONTMATTER = re.compile(r"\A﻿?---\r?\n.*?\r?\n---[ \t]*(\r?\n|\Z)", re.DOTALL)


class InvalidContent(ValueError):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


class NoteExists(Exception):
    pass


def append_text(current, text):
    """Append ``text`` on its own line(s), keeping the note's newline style.

    windows_client/applier.py implements the same rule, so the server copy and
    the file on the PC end up byte-identical.
    """
    newline = "\r\n" if "\r\n" in current else "\n"
    text = text.replace("\r\n", "\n").replace("\n", newline)
    if current and not current.endswith("\n"):
        current += newline
    if not text.endswith(newline):
        text += newline
    return current + text


def has_frontmatter(content):
    return bool(_FRONTMATTER.match(content))


def _check_content(content, *, allow_empty=False):
    if not isinstance(content, str):
        raise InvalidContent("invalid_content", "content must be a string")
    if not allow_empty and not content.strip():
        raise InvalidContent("invalid_content", "content is empty")
    if "\x00" in content:
        raise InvalidContent("invalid_content", "content contains NUL characters")
    try:
        size = len(content.encode("utf-8"))
    except UnicodeEncodeError:
        raise InvalidContent("invalid_content", "content is not valid Unicode") from None
    if size > settings.VAULT_MAX_NOTE_BYTES:
        raise InvalidContent("too_large", f"note would exceed {settings.VAULT_MAX_NOTE_BYTES} bytes")


def _hash(content):
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def _store(path, content, now, note=None):
    """Create or replace the server copy of a note."""
    if note is None:
        note = VaultNote(path=path, title=title_of(path)[:255], folder=folder_of(path), created_at=now)
    note.content = content
    note.content_hash = _hash(content)
    note.updated_at = now
    note.last_synced_at = now
    note.save()
    return note


def _queue(op, path, content, client, base_hash="", detail=""):
    return PendingChange.objects.create(
        op=op, path=path, content=content, client=client, base_hash=base_hash, detail=detail[:255]
    )


def _path_taken(path):
    return (
        VaultNote.objects.filter(path=path).exists()
        or PendingChange.objects.filter(path=path, op=PendingChange.OP_CREATE, status=PendingChange.PENDING).exists()
    )


def _result(status, note, change, **extra):
    return {
        "status": status,
        "path": note.path,
        "content_hash": note.content_hash,
        "updated_at": timezone.localtime(note.updated_at).isoformat(),
        "change_id": change.pk,
        "applied_to_pc": False,
        **extra,
    }


def create_note(content, *, client, folder=None, title=None, path=None):
    """Create a new note from ``path``, or from ``folder`` + ``title``. Never overwrites."""
    path = normalize_note_path(path) if path else note_path_from_title(folder, title)
    ensure_writable(path)
    _check_content(content)
    now = timezone.now()
    with transaction.atomic():
        if _path_taken(path):
            raise NoteExists(path)
        note = _store(path, content, now)
        change = _queue(PendingChange.OP_CREATE, path, content, client)
    return _result(QUEUED, note, change)


def append_note(path, content, *, client):
    """Add text to the end of an existing note (frontmatter and body above are untouched)."""
    path = ensure_writable(normalize_note_path(path))
    _check_content(content)
    now = timezone.now()
    with transaction.atomic():
        note = VaultNote.objects.select_for_update().filter(path=path).first()
        if note is None:
            raise NoteNotFound(path)
        base_hash = note.content_hash
        new_content = append_text(note.content, content)
        _check_content(new_content)
        _store(path, new_content, now, note)
        change = _queue(PendingChange.OP_APPEND, path, content, client, base_hash=base_hash)
    return _result(QUEUED, note, change)


def update_note(path, content, *, client, base_hash, base_updated_at=None):
    """Replace a note only if it still has ``base_hash`` (and ``base_updated_at``, if given)."""
    path = ensure_writable(normalize_note_path(path))
    _check_content(content)
    if not isinstance(base_hash, str) or not _HASH.match(base_hash):
        raise InvalidContent("invalid_base_hash", "base_hash (content_hash from read_note) is required")
    expected_time = None
    if base_updated_at:
        expected_time = parse_datetime(base_updated_at) if isinstance(base_updated_at, str) else None
        if expected_time is None:
            raise InvalidContent("invalid_base_updated_at", "base_updated_at must be an ISO 8601 datetime")

    now = timezone.now()
    with transaction.atomic():
        note = VaultNote.objects.select_for_update().filter(path=path).first()
        if note is None:
            raise NoteNotFound(path)
        stale = note.content_hash != base_hash.lower() or (
            expected_time is not None and expected_time != note.updated_at
        )
        if stale:
            # Keep the AI's text, but next to the note instead of over it.
            conflict_path = _free_conflict_path(path, now)
            conflict_note = _store(conflict_path, content, now)
            change = _queue(PendingChange.OP_CREATE, conflict_path, content, client, detail=f"conflict_of:{path}")
            return _result(
                CONFLICT,
                conflict_note,
                change,
                original_path=path,
                current_hash=note.content_hash,
                message="The note changed since it was read; the new text was saved as a conflict note.",
            )
        if has_frontmatter(note.content) and not has_frontmatter(content):
            raise InvalidContent("frontmatter_removed", "the note has YAML frontmatter; keep it in the new content")
        _store(path, content, now, note)
        change = _queue(PendingChange.OP_UPDATE, path, content, client, base_hash=base_hash.lower())
    return _result(QUEUED, note, change)


def _free_conflict_path(path, now):
    for attempt in range(1, 100):
        candidate = conflict_path_for(path, timezone.localtime(now), attempt)
        if not _path_taken(candidate):
            return normalize_note_path(candidate)
    raise InvalidPath("could not find a free conflict file name")
