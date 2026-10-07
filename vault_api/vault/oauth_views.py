"""HTTP endpoints of the OAuth server (mounted at the site root, see oauth_urls.py)."""
import json
from urllib.parse import urlparse

from django.conf import settings
from django.http import HttpResponse, HttpResponseRedirect, JsonResponse
from django.middleware.csrf import get_token
from django.utils.html import escape
from django.views.decorators.csrf import csrf_exempt

from . import oauth
from .services import audit

AUTH_FIELDS = ("response_type", "client_id", "redirect_uri", "state", "code_challenge", "code_challenge_method", "scope", "resource")


def _json(data, status=200):
    response = JsonResponse(data, status=status, json_dumps_params={"ensure_ascii": False})
    response["Cache-Control"] = "no-store"
    return response


def _oauth_error(exc):
    return _json({"error": exc.error, "error_description": exc.description}, status=exc.status)


def _insecure(request):
    return settings.VAULT_REQUIRE_HTTPS and not request.is_secure()


def _get_only(view):
    def wrapper(request):
        if request.method != "GET":
            return _json({"error": "invalid_request", "error_description": "use GET"}, status=405)
        return view(request)

    wrapper.login_required = False
    return wrapper


@_get_only
def protected_resource_metadata(request):
    return _json(oauth.protected_resource_metadata(request))


@_get_only
def authorization_server_metadata(request):
    return _json(oauth.authorization_server_metadata(request))


@csrf_exempt
def register(request):
    if request.method != "POST":
        return _json({"error": "invalid_request", "error_description": "use POST"}, status=405)
    if _insecure(request):
        return _json({"error": "invalid_request", "error_description": "HTTPS is required"}, status=403)
    try:
        data = json.loads(request.body or b"null")
        result = oauth.register_client(data)
    except ValueError:
        return _json({"error": "invalid_client_metadata", "error_description": "invalid JSON"}, status=400)
    except oauth.OAuthError as exc:
        audit.record(request, "oauth_register", success=False, detail=exc.error)
        return _oauth_error(exc)
    audit.record(request, "oauth_register", success=True, client=result["client_name"][:64], detail=result["client_id"][:12])
    return _json(result, status=201)


@csrf_exempt  # public clients authenticate with PKCE, not cookies
def token(request):
    if request.method != "POST":
        return _json({"error": "invalid_request", "error_description": "use POST"}, status=405)
    if _insecure(request):
        return _json({"error": "invalid_request", "error_description": "HTTPS is required"}, status=403)
    try:
        result = oauth.token_response(request.POST, request)
    except oauth.OAuthError as exc:
        audit.record(request, "oauth_token", success=False, detail=f"{request.POST.get('grant_type', '')}:{exc.error}"[:255])
        return _oauth_error(exc)
    audit.record(request, "oauth_token", success=True, detail=f"{request.POST.get('grant_type')} scope={result['scope']}")
    return _json(result)


def authorize(request):
    """Consent screen: the owner approves the connection with their password."""
    if request.method not in ("GET", "POST"):
        return _page("エラー", "<p>GET または POST を使ってください。</p>", status=405)
    if _insecure(request):
        return _page("エラー", "<p>HTTPS で開いてください。</p>", status=403)
    params = request.GET if request.method == "GET" else request.POST
    try:
        auth_request = oauth.parse_authorization_request(params, request)
    except oauth.OAuthError as exc:
        if exc.redirectable and oauth.OAuthClient.objects.filter(client_id=params.get("client_id") or "").exists():
            uri = params.get("redirect_uri")
            if uri and oauth.redirect_uri_allowed(uri):
                return _redirect(uri, request, error=exc.error, error_description=exc.description, state=params.get("state"))
        return _page("接続できません", f"<p>{escape(exc.description)}</p>", status=400)

    if request.method == "GET":
        return _consent_page(request, params, auth_request)

    if request.POST.get("decision") != "allow":
        audit.record(request, "oauth_login", success=True, client=auth_request.client.client_name[:64], detail="denied")
        return _redirect(auth_request.redirect_uri, request, error="access_denied", state=auth_request.state)
    if not oauth.owner_password_configured():
        return _consent_page(request, params, auth_request, error="サーバーにパスワードが設定されていません（VAULT_OWNER_PASSWORD_HASH）。")
    if oauth.login_locked():
        return _consent_page(request, params, auth_request, error="失敗が続いたため一時的にロックしています。15分ほど待ってから試してください。")
    if not oauth.check_owner_password(request.POST.get("password")):
        audit.record(request, "oauth_login", success=False, client=auth_request.client.client_name[:64], detail="bad_password")
        return _consent_page(request, params, auth_request, error="パスワードが違います。")

    scopes = [oauth.SCOPE_READ]
    if request.POST.get("allow_write") == "1" and oauth.SCOPE_WRITE in auth_request.requested_scopes:
        scopes.append(oauth.SCOPE_WRITE)
    code = oauth.issue_code(auth_request, scopes)
    audit.record(
        request, "oauth_login", success=True, client=auth_request.client.client_name[:64],
        detail=f"granted {oauth.granted_scope_string(scopes)}",
    )
    return _redirect(auth_request.redirect_uri, request, code=code, state=auth_request.state)


authorize.login_required = False


def _redirect(uri, request, **params):
    params["iss"] = oauth.issuer(request)
    if not params.get("state"):
        params.pop("state", None)
    response = HttpResponseRedirect(oauth.add_query(uri, params))
    response["Cache-Control"] = "no-store"
    return response


def _consent_page(request, params, auth_request, error=""):
    client_name = auth_request.client.client_name or "名前のないアプリ"
    redirect_host = urlparse(auth_request.redirect_uri).hostname or ""
    hidden = "".join(
        f'<input type="hidden" name="{name}" value="{escape(params.get(name, ""))}">' for name in AUTH_FIELDS
    )
    wants_write = oauth.SCOPE_WRITE in auth_request.requested_scopes
    write_option = (
        '<label class="opt"><input type="checkbox" name="allow_write" value="1" checked> '
        "書き込みも許可する（ノートの作成・追記・更新。削除はできません）</label>"
        if wants_write
        else ""
    )
    error_html = f'<p class="error">{escape(error)}</p>' if error else ""
    body = f"""
<p><strong>{escape(client_name)}</strong> が Obsidian WorkVault への接続を求めています。</p>
<p class="host">許可後の戻り先: <strong>{escape(redirect_host)}</strong></p>
{error_html}
<form method="post">
  <input type="hidden" name="csrfmiddlewaretoken" value="{escape(get_token(request))}">
  {hidden}
  <label class="opt"><input type="checkbox" checked disabled> ノートの検索・閲覧</label>
  {write_option}
  <label for="password">Vaultのパスワード</label>
  <input id="password" type="password" name="password" autocomplete="current-password" autofocus>
  <div class="buttons">
    <button type="submit" name="decision" value="allow" class="allow">許可する</button>
    <button type="submit" name="decision" value="deny" class="deny">拒否</button>
  </div>
</form>
<p class="note">心当たりがない場合は「拒否」を押してください。</p>
"""
    return _page("接続の許可", body)


def _page(title, body, status=200):
    html = f"""<!doctype html>
<html lang="ja"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{escape(title)} - Obsidian WorkVault</title>
<style>
body {{ font-family: system-ui, -apple-system, sans-serif; background: #f5f5f4; color: #1c1917; margin: 0; padding: 24px 16px; }}
main {{ max-width: 420px; margin: 0 auto; background: #fff; border-radius: 12px; padding: 24px; box-shadow: 0 1px 3px rgba(0,0,0,.1); }}
h1 {{ font-size: 1.25rem; margin-top: 0; }}
label {{ display: block; margin: 12px 0 4px; }}
.opt {{ display: flex; gap: 8px; align-items: flex-start; }}
input[type=password] {{ width: 100%; box-sizing: border-box; padding: 10px; font-size: 1rem; border: 1px solid #a8a29e; border-radius: 8px; }}
.buttons {{ display: flex; gap: 8px; margin-top: 16px; }}
button {{ flex: 1; padding: 12px; font-size: 1rem; border-radius: 8px; border: 0; cursor: pointer; }}
.allow {{ background: #4f46e5; color: #fff; }}
.deny {{ background: #e7e5e4; color: #1c1917; }}
.error {{ color: #b91c1c; font-weight: bold; }}
.host, .note {{ color: #57534e; font-size: .9rem; }}
</style></head>
<body><main><h1>{escape(title)}</h1>{body}</main></body></html>"""
    response = HttpResponse(html, status=status)
    response["Cache-Control"] = "no-store"
    response["Content-Security-Policy"] = "default-src 'none'; style-src 'unsafe-inline'; frame-ancestors 'none'; base-uri 'none'"
    # Not "no-referrer": that turns the form POST's Origin into "null" and fails Django's CSRF check.
    response["Referrer-Policy"] = "same-origin"
    return response
