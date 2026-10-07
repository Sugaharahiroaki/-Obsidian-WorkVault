"""HTTP endpoints of the Vault API.

Phase 1: the Windows client pushes notes (sync scope); AI clients only read
(read scope). There is deliberately no delete endpoint.
"""
import json
from functools import wraps

from django.conf import settings
from django.core.exceptions import RequestDataTooBig
from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt

from . import auth
from .security import InvalidPath
from .services import audit, notes, sync

JSON_PARAMS = {"ensure_ascii": False}


def _json(data, status=200):
    return JsonResponse(data, status=status, json_dumps_params=JSON_PARAMS, safe=isinstance(data, dict))


def _error(status, code, message):
    return _json({"error": {"code": code, "message": message}}, status=status)


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

        return wrapper

    return decorator


@api_endpoint("sync", scope=auth.SCOPE_SYNC, method="POST")
def sync_view(request, key):
    if request.content_type != "application/json":
        audit.record(request, "sync", success=False, client=key.client, detail="unsupported_media_type")
        return _error(415, "unsupported_media_type", "Send application/json.")
    try:
        payload = json.loads(request.body)
    except RequestDataTooBig:
        audit.record(request, "sync", success=False, client=key.client, detail="request_too_large")
        return _error(413, "request_too_large", "Request body is too large; send smaller batches.")
    except (ValueError, UnicodeDecodeError):
        audit.record(request, "sync", success=False, client=key.client, detail="invalid_json")
        return _error(400, "invalid_json", "Body is not valid JSON.")

    items = payload.get("notes") if isinstance(payload, dict) else None
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
        note = notes.read_note(raw_path)
    except InvalidPath as exc:
        audit.record(request, "read", success=False, path=raw_path, client=key.client, detail="invalid_path")
        return _error(400, "invalid_path", str(exc))
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


def not_found(request, exception=None):
    return _error(404, "not_found", "Not found.")


def server_error(request):
    return _error(500, "server_error", "Internal server error.")
