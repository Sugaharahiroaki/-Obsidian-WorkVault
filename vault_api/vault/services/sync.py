"""Upsert of notes pushed by the Windows sync client.

Phase 1 is one-way (PC -> server): the PC is the source of truth, so a
changed note simply replaces the stored copy. Nothing is ever deleted.
"""
import hashlib
from datetime import timedelta

from django.conf import settings
from django.db import IntegrityError, transaction
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from ..models import VaultNote
from ..security import InvalidPath, folder_of, normalize_note_path, title_of

CREATED = "created"
UPDATED = "updated"
UNCHANGED = "unchanged"
REJECTED = "rejected"

_MAX_CLOCK_SKEW = timedelta(days=1)


class ItemError(ValueError):
    def __init__(self, code):
        super().__init__(code)
        self.code = code


def sync_notes(items):
    """Apply each item independently; one bad item does not fail the batch.

    Each item: {"path", "content", "content_hash"?, "created_at"?, "modified_at"?}
    Returns one {"path", "status", "error"?} dict per item, in order.
    """
    results = []
    for item in items:
        raw_path = item.get("path") if isinstance(item, dict) else None
        display_path = raw_path[:1024] if isinstance(raw_path, str) else ""
        try:
            if not isinstance(item, dict):
                raise ItemError("invalid_item")
            path, status = _sync_one(item)
            results.append({"path": path, "status": status})
        except InvalidPath:
            results.append({"path": display_path, "status": REJECTED, "error": "invalid_path"})
        except ItemError as exc:
            results.append({"path": display_path, "status": REJECTED, "error": exc.code})
    return results


def _sync_one(item):
    path = normalize_note_path(item.get("path"))

    content = item.get("content")
    if not isinstance(content, str):
        raise ItemError("invalid_content")
    if "\x00" in content:
        raise ItemError("invalid_content")
    try:
        encoded = content.encode("utf-8")
    except UnicodeEncodeError:  # lone surrogates from JSON escapes
        raise ItemError("invalid_content") from None
    if len(encoded) > settings.VAULT_MAX_NOTE_BYTES:
        raise ItemError("too_large")

    content_hash = hashlib.sha256(encoded).hexdigest()
    claimed = item.get("content_hash")
    if claimed is not None and (not isinstance(claimed, str) or claimed.lower() != content_hash):
        raise ItemError("hash_mismatch")

    now = timezone.now()
    created_at = _client_time(item.get("created_at"), now)
    modified_at = _client_time(item.get("modified_at"), now)

    try:
        with transaction.atomic():
            note = VaultNote.objects.select_for_update().filter(path=path).first()
            if note is None:
                VaultNote.objects.create(
                    path=path,
                    title=title_of(path)[:255],
                    folder=folder_of(path),
                    content=content,
                    content_hash=content_hash,
                    created_at=min(created_at, modified_at),
                    updated_at=modified_at,
                    last_synced_at=now,
                )
                return path, CREATED
            if note.content_hash == content_hash:
                note.last_synced_at = now
                note.save(update_fields=["last_synced_at"])
                return path, UNCHANGED
            note.content = content
            note.content_hash = content_hash
            note.title = title_of(path)[:255]
            note.folder = folder_of(path)
            note.updated_at = modified_at
            note.last_synced_at = now
            note.save()
            return path, UPDATED
    except IntegrityError:  # concurrent create of the same path
        raise ItemError("conflict_retry") from None


def _client_time(value, now):
    """Parse a client timestamp; fall back to now if missing, naive or in the future."""
    if not isinstance(value, str):
        return now
    try:
        parsed = parse_datetime(value)
    except ValueError:
        return now
    if parsed is None or timezone.is_naive(parsed) or parsed > now + _MAX_CLOCK_SKEW:
        return now
    return parsed
