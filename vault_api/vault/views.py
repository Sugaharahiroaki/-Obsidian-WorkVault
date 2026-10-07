"""HTTP endpoints of the Vault API.

- Windows client (sync scope): pushes notes, pulls and acknowledges AI writes.
- AI clients: read (read scope); create/append/update inside the writable
  folders (write scope).
There is deliberately no delete endpoint.
"""
import json
from functools import wraps

from django.conf import settings
from django.core.exceptions import RequestDataTooBig
from django.http import JsonResponse
from django.urls import reverse
from django.views.decorators.csrf import csrf_exempt

from . import auth, openapi
from .security import InvalidPath
from .services import audit, changes, notes, sync, writes

JSON_PARAMS = {"ensure_ascii": False}


def _json(data, status=200):
    return JsonResponse(data, status=status, json_dumps_params=JSON_PARAMS, safe=isinstance(data, dict))


def _error(status, code, message):
    return _json({"error": {"code": code, "message": message}}, status=status)


class BadRequest(Exception):
    def __init__(self, status, code, message):
        super().__init__(message)
        self.response = _error(status, code, message)
        self.code = code


def _read_json(request):
    """Parse a JSON object body or raise BadRequest."""
    if request.content_type != "application/json":
        raise BadRequest(415, "unsupported_media_type", "Send application/json.")
    try:
        payload = json.loads(request.body)
    except RequestDataTooBig:
        raise BadRequest(413, "request_too_large", "Request body is too large; send smaller batches.") from None
    except (ValueError, UnicodeDecodeError):
        raise BadRequest(400, "invalid_json", "Body is not valid JSON.") from None
    if not isinstance(payload, dict):
        raise BadRequest(400, "invalid_payload", "Expected a JSON object.")
    return payload


def api_endpoint(action, *, scope, method):
    """Method check, HTTPS enforcement, API-key auth and scope check.

    The wrapped view receives the authenticated ApiKey and is responsible
    for logging its own outcome.
    """

    def decorator(view):
        @csrf_exempt  # Bearer-token API without cookies: CSRF does not apply.
        @wraps(view)
        def wrapper(request):
            if request.method != method:
                response = _error(405, "method_not_allowed", f"Use {method}.")
                response["Allow"] = method
                return response
            if settings.VAULT_REQUIRE_HTTPS and not request.is_secure():
                audit.record(request, action, success=False, detail="https_required")
                return _error(403, "https_required", "HTTPS is required.")
            key = auth.authenticate(request)
            if key is None:
                audit.record(request, action, success=False, detail="unauthorized")
                response = _error(401, "unauthorized", "Missing or invalid API key.")
                response["WWW-Authenticate"] = 'Bearer realm="vault"'
                return response
            if scope not in key.scopes:
                audit.record(request, action, success=False, client=key.client, detail="forbidden")
                return _error(403, "forbidden", "This API key may not perform this action.")
            return view(request, key)

        # Host projects using LoginRequiredMiddleware must not redirect API calls to a login page.
        wrapper.login_required = False
        return wrapper

    return decorator


@api_endpoint("sync", scope=auth.SCOPE_SYNC, method="POST")
def sync_view(request, key):
    try:
        payload = _read_json(request)
    except BadRequest as exc:
        audit.record(request, "sync", success=False, client=key.client, detail=exc.code)
        return exc.response

    items = payload.get("notes")
    if not isinstance(items, list):
        audit.record(request, "sync", success=False, client=key.client, detail="invalid_payload")
        return _error(400, "invalid_payload", 'Expected {"notes": [...]}.')
    if len(items) > settings.VAULT_MAX_BATCH_NOTES:
        audit.record(request, "sync", success=False, client=key.client, detail="batch_too_large")
        return _error(413, "batch_too_large", f"At most {settings.VAULT_MAX_BATCH_NOTES} notes per request.")

    results = sync.sync_notes(items)
    audit.record_many(
        request,
        "sync",
        [(r["path"], r["status"] != sync.REJECTED, r.get("error") or r["status"]) for r in results],
        client=key.client,
    )
    summary = {status: 0 for status in (sync.CREATED, sync.UPDATED, sync.UNCHANGED, sync.REJECTED)}
    for r in results:
        summary[r["status"]] += 1
    return _json({"results": results, "summary": summary})


@api_endpoint("search", scope=auth.SCOPE_READ, method="GET")
def search_view(request, key):
    try:
        result = notes.search_notes(
            request.GET.get("q", ""),
            limit=request.GET.get("limit"),
            folder=request.GET.get("folder"),
        )
    except (notes.InvalidQuery, InvalidPath) as exc:
        audit.record(request, "search", success=False, client=key.client, detail="invalid_query")
        return _error(400, "invalid_query", str(exc))
    # The query itself is not logged: it can be as sensitive as the notes.
    audit.record(
        request, "search", success=True, client=key.client, detail=f"mode={result['mode']} hits={result['count']}"
    )
    return _json(result)


@api_endpoint("read", scope=auth.SCOPE_READ, method="GET")
def note_view(request, key):
    raw_path = request.GET.get("path", "")
    try:
        note = notes.read_note(raw_path, offset=request.GET.get("offset"), max_chars=request.GET.get("max_chars"))
    except InvalidPath as exc:
        audit.record(request, "read", success=False, path=raw_path, client=key.client, detail="invalid_path")
        return _error(400, "invalid_path", str(exc))
    except notes.InvalidQuery as exc:
        audit.record(request, "read", success=False, path=raw_path, client=key.client, detail="invalid_query")
        return _error(400, "invalid_query", str(exc))
    except notes.NoteNotFound:
        audit.record(request, "read", success=False, path=raw_path, client=key.client, detail="not_found")
        return _error(404, "not_found", "Note not found.")
    audit.record(request, "read", success=True, path=note["path"], client=key.client)
    return _json(note)


@api_endpoint("tree", scope=auth.SCOPE_READ, method="GET")
def tree_view(request, key):
    folder = request.GET.get("folder")
    try:
        tree = notes.get_tree(folder)
    except InvalidPath as exc:
        audit.record(request, "tree", success=False, path=folder or "", client=key.client, detail="invalid_path")
        return _error(400, "invalid_path", str(exc))
    audit.record(request, "tree", success=True, path=tree["path"], client=key.client)
    return _json(tree)


@api_endpoint("recent", scope=auth.SCOPE_READ, method="GET")
def recent_view(request, key):
    folder = request.GET.get("folder")
    try:
        recent = notes.get_recent_notes(limit=request.GET.get("limit"), folder=folder)
    except (notes.InvalidQuery, InvalidPath) as exc:
        audit.record(request, "recent", success=False, path=folder or "", client=key.client, detail="invalid_query")
        return _error(400, "invalid_query", str(exc))
    audit.record(request, "recent", success=True, path=folder or "", client=key.client, detail=f"count={len(recent)}")
    return _json({"count": len(recent), "results": recent})


@api_endpoint("list", scope=auth.SCOPE_READ, method="GET")
def list_view(request, key):
    folder = request.GET.get("folder")
    try:
        listed = notes.list_notes(folder)
    except InvalidPath as exc:
        audit.record(request, "list", success=False, path=folder or "", client=key.client, detail="invalid_path")
        return _error(400, "invalid_path", str(exc))
    audit.record(request, "list", success=True, path=folder or "", client=key.client, detail=f"count={len(listed)}")
    return _json({"count": len(listed), "results": listed})


# --- Phase 3: writes from AI clients ---------------------------------------


def _write(request, key, action, perform, success_status):
    """Shared handling for create/append/update. Bodies are never logged."""
    raw_path = ""
    try:
        payload = _read_json(request)
        raw_path = payload.get("path") if isinstance(payload.get("path"), str) else ""
        result = perform(payload)
    except BadRequest as exc:
        audit.record(request, action, success=False, path=raw_path, client=key.client, detail=exc.code)
        return exc.response
    except InvalidPath as exc:
        audit.record(request, action, success=False, path=raw_path, client=key.client, detail="invalid_path")
        return _error(400, "invalid_path", str(exc))
    except writes.InvalidContent as exc:
        status = 422 if exc.code == "frontmatter_removed" else 413 if exc.code == "too_large" else 400
        audit.record(request, action, success=False, path=raw_path, client=key.client, detail=exc.code)
        return _error(status, exc.code, str(exc))
    except notes.NoteNotFound:
        audit.record(request, action, success=False, path=raw_path, client=key.client, detail="not_found")
        return _error(404, "not_found", "Note not found. Use createNote for new notes.")
    except writes.NoteExists as exc:
        audit.record(request, action, success=False, path=str(exc), client=key.client, detail="exists")
        return _error(409, "exists", "A note with this path already exists; use appendNote or updateNote.")

    if result["status"] == writes.CONFLICT:
        audit.record(
            request, action, success=False, path=result["original_path"], client=key.client,
            detail=f"conflict saved_as={result['path']}",
        )
        return _json(result, status=409)
    audit.record(request, action, success=True, path=result["path"], client=key.client, detail=f"change={result['change_id']}")
    return _json(result, status=success_status)


@api_endpoint("create", scope=auth.SCOPE_WRITE, method="POST")
def create_view(request, key):
    def perform(payload):
        return writes.create_note(
            payload.get("content"),
            client=key.client,
            folder=payload.get("folder"),
            title=payload.get("title"),
            path=payload.get("path"),
        )

    return _write(request, key, "create", perform, 201)


@api_endpoint("append", scope=auth.SCOPE_WRITE, method="POST")
def append_view(request, key):
    def perform(payload):
        return writes.append_note(payload.get("path"), payload.get("content"), client=key.client)

    return _write(request, key, "append", perform, 200)


@api_endpoint("update", scope=auth.SCOPE_WRITE, method="POST")
def update_view(request, key):
    def perform(payload):
        return writes.update_note(
            payload.get("path"),
            payload.get("content"),
            client=key.client,
            base_hash=payload.get("base_hash"),
            base_updated_at=payload.get("base_updated_at"),
        )

    return _write(request, key, "update", perform, 200)


# --- Phase 3: change queue for the Windows client ---------------------------


@api_endpoint("pull", scope=auth.SCOPE_SYNC, method="GET")
def changes_view(request, key):
    pending = changes.pending_changes()
    audit.record(request, "pull", success=True, client=key.client, detail=f"count={len(pending)}")
    return _json({"count": len(pending), "changes": pending})


@api_endpoint("ack", scope=auth.SCOPE_SYNC, method="POST")
def changes_ack_view(request, key):
    try:
        payload = _read_json(request)
    except BadRequest as exc:
        audit.record(request, "ack", success=False, client=key.client, detail=exc.code)
        return exc.response
    items = payload.get("results")
    if not isinstance(items, list):
        audit.record(request, "ack", success=False, client=key.client, detail="invalid_payload")
        return _error(400, "invalid_payload", 'Expected {"results": [...]}.')
    outcome = changes.acknowledge(items)
    audit.record_many(
        request,
        "ack",
        [(f"change={o['id']}", o["ok"], o.get("error", "")) for o in outcome],
        client=key.client,
    )
    return _json({"results": outcome})


# --- Schema -----------------------------------------------------------------


def openapi_view(request):
    """Public (no data inside): lets ChatGPT import the Action schema by URL."""
    if request.method != "GET":
        response = _error(405, "method_not_allowed", "Use GET.")
        response["Allow"] = "GET"
        return response
    base_path = reverse("vault:search").rsplit("search/", 1)[0].rstrip("/")
    root = settings.VAULT_PUBLIC_URL or request.build_absolute_uri("/").rstrip("/")
    return _json(openapi.build_schema(root + base_path))


openapi_view.login_required = False


def not_found(request, exception=None):
    return _error(404, "not_found", "Not found.")


def server_error(request):
    return _error(500, "server_error", "Internal server error.")
