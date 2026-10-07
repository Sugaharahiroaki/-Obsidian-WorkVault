"""Read-side operations on mirrored notes.

These functions are the single implementation behind the HTTP endpoints and
are shaped to be exposed directly as AI tools in Phase 2:

    search_notes(query)      read_note(path)
    list_notes(folder)       get_recent_notes(limit)

Path validation happens here, not in the views, so every caller gets it.
All functions return plain JSON-serialisable values.
"""
from django.db.models import Q
from django.utils import timezone

from .. import search
from ..models import VaultNote
from ..security import normalize_folder_path, normalize_note_path

DEFAULT_LIMIT = 20
MAX_LIMIT = 100
MAX_QUERY_CHARS = 200
MAX_READ_CHARS = 200_000


class NoteNotFound(LookupError):
    pass


class InvalidQuery(ValueError):
    pass


def clamp_limit(value, default=DEFAULT_LIMIT):
    if value in (None, ""):
        return default
    try:
        number = int(value)
    except (TypeError, ValueError):
        raise InvalidQuery("limit must be an integer") from None
    return max(1, min(MAX_LIMIT, number))


def _int_param(value, name, *, minimum, maximum, default):
    if value in (None, ""):
        return default
    try:
        number = int(value)
    except (TypeError, ValueError):
        raise InvalidQuery(f"{name} must be an integer") from None
    if not minimum <= number <= maximum:
        raise InvalidQuery(f"{name} must be between {minimum} and {maximum}")
    return number


def _iso(dt):
    return timezone.localtime(dt).isoformat() if dt else None


def _summary(note):
    return {
        "path": note.path,
        "title": note.title,
        "folder": note.folder,
        "updated_at": _iso(note.updated_at),
    }


def _in_folder(folder):
    return Q(folder=folder) | Q(folder__startswith=folder + "/")


def search_notes(query, limit=DEFAULT_LIMIT, folder=None):
    if query is not None and not isinstance(query, str):
        raise InvalidQuery("query must be a string")
    query = (query or "").strip()
    if not query:
        raise InvalidQuery("query is empty")
    if len(query) > MAX_QUERY_CHARS:
        raise InvalidQuery(f"query is longer than {MAX_QUERY_CHARS} characters")
    folder = normalize_folder_path(folder)
    result = search.search(query, limit=clamp_limit(limit), folder=folder)
    return {
        "mode": result.mode,
        "count": len(result.notes),
        "results": [
            {**_summary(note), "snippet": search.make_snippet(note.content, result.terms)}
            for note in result.notes
        ],
    }


def read_note(path, offset=None, max_chars=None):
    """Whole note by default; ``offset``/``max_chars`` page through long notes.

    ``content_hash`` always describes the whole note (use it as base_hash for
    update_note), even when only a slice of ``content`` is returned.
    """
    path = normalize_note_path(path)
    offset = _int_param(offset, "offset", minimum=0, maximum=10**9, default=0)
    max_chars = _int_param(max_chars, "max_chars", minimum=1, maximum=MAX_READ_CHARS, default=None)
    note = VaultNote.objects.filter(path=path).first()
    if note is None:
        raise NoteNotFound(path)
    total = len(note.content)
    end = total if max_chars is None else min(total, offset + max_chars)
    return {
        **_summary(note),
        "content": note.content[offset:end],
        "content_hash": note.content_hash,
        "created_at": _iso(note.created_at),
        "last_synced_at": _iso(note.last_synced_at),
        "total_chars": total,
        "offset": min(offset, total),
        "truncated": end < total,
        "next_offset": end if end < total else None,
    }


def list_notes(folder=None):
    folder = normalize_folder_path(folder)
    qs = VaultNote.objects.only("path", "title", "folder", "updated_at").order_by("path")
    if folder:
        qs = qs.filter(_in_folder(folder))
    return [_summary(note) for note in qs]


def get_recent_notes(limit=DEFAULT_LIMIT, folder=None):
    folder = normalize_folder_path(folder)
    qs = VaultNote.objects.only("path", "title", "folder", "updated_at").order_by("-updated_at", "path")
    if folder:
        qs = qs.filter(_in_folder(folder))
    return [_summary(note) for note in qs[: clamp_limit(limit)]]


def get_tree(folder=None):
    """Nested folder/note tree of everything synced (empty folders do not exist here)."""
    root_path = normalize_folder_path(folder)
    root = _folder_node(root_path)
    folders = {root_path: root}

    for note in list_notes(root_path):
        parent = _ensure_folder(folders, note["folder"], root_path)
        parent["children"].append(
            {
                "type": "note",
                "name": note["path"].rsplit("/", 1)[-1],
                "path": note["path"],
                "title": note["title"],
                "updated_at": note["updated_at"],
            }
        )

    _sort_tree(root)
    return root


def _folder_node(path):
    return {"type": "folder", "name": path.rsplit("/", 1)[-1], "path": path, "children": []}


def _ensure_folder(folders, path, root_path):
    if path in folders:
        return folders[path]
    parent_path = path.rsplit("/", 1)[0] if "/" in path else ""
    if len(parent_path) < len(root_path):
        parent_path = root_path
    parent = _ensure_folder(folders, parent_path, root_path)
    node = _folder_node(path)
    parent["children"].append(node)
    folders[path] = node
    return node


def _sort_tree(node):
    node["children"].sort(key=lambda child: (child["type"] != "folder", child["name"].lower()))
    for child in node["children"]:
        if child["type"] == "folder":
            _sort_tree(child)
