"""OAuth 2.1 authorization server for MCP clients (ChatGPT / Claude apps).

Only what MCP clients need: protected-resource and authorization-server
metadata (RFC 9728 / RFC 8414), Dynamic Client Registration for public
clients (RFC 7591), the authorization-code grant with mandatory S256 PKCE,
refresh-token rotation, and the ``iss`` response parameter (RFC 9207).

The only user is the vault owner, who approves each connection with a
password (hash in VAULT_OWNER_PASSWORD_HASH). Codes and tokens are opaque
random strings stored as SHA-256 hashes.
"""
import base64
import hashlib
import secrets
from dataclasses import dataclass
from datetime import timedelta
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

from django.conf import settings
from django.contrib.auth.hashers import check_password
from django.db import transaction
from django.urls import reverse
from django.utils import timezone

from .models import AccessLog, OAuthClient, OAuthCode, OAuthToken

SCOPE_READ = "vault:read"
SCOPE_WRITE = "vault:write"
SCOPES = (SCOPE_READ, SCOPE_WRITE)
OFFLINE_ACCESS = "offline_access"
CODE_LIFETIME = timedelta(minutes=5)
FAILED_LOGIN_WINDOW = timedelta(minutes=15)
LOOPBACK_HOSTS = ("localhost", "127.0.0.1")


class OAuthError(Exception):
    """``redirectable`` errors go back to the client; others are shown to the user."""

    def __init__(self, error, description, *, status=400, redirectable=False):
        super().__init__(description)
        self.error = error
        self.description = description
        self.status = status
        self.redirectable = redirectable


# --- URLs -------------------------------------------------------------------


def issuer(request):
    return settings.VAULT_PUBLIC_URL or request.build_absolute_uri("/").rstrip("/")


def mcp_resource(request):
    """The MCP server URL exactly as users enter it in ChatGPT / Claude."""
    return issuer(request) + reverse("vault:mcp")


def resource_metadata_url(request):
    return issuer(request) + "/.well-known/oauth-protected-resource" + reverse("vault:mcp")


def protected_resource_metadata(request):
    return {
        "resource": mcp_resource(request),
        "authorization_servers": [issuer(request)],
        "scopes_supported": list(SCOPES),
        "bearer_methods_supported": ["header"],
        "resource_name": "Obsidian WorkVault",
    }


def authorization_server_metadata(request):
    base = issuer(request)
    return {
        "issuer": base,
        "authorization_endpoint": base + reverse("vault-oauth:authorize"),
        "token_endpoint": base + reverse("vault-oauth:token"),
        "registration_endpoint": base + reverse("vault-oauth:register"),
        "response_types_supported": ["code"],
        "grant_types_supported": ["authorization_code", "refresh_token"],
        "code_challenge_methods_supported": ["S256"],
        "token_endpoint_auth_methods_supported": ["none"],
        "scopes_supported": [*SCOPES, OFFLINE_ACCESS],
        "authorization_response_iss_parameter_supported": True,
    }


# --- Helpers ----------------------------------------------------------------


def _hash(value):
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _new_secret():
    return secrets.token_urlsafe(32)


def redirect_uri_allowed(uri):
    """https to an allowed host (chatgpt.com, claude.ai, ...) or a loopback URL for Claude Code."""
    if not isinstance(uri, str) or len(uri) > 1024:
        return False
    parsed = urlparse(uri)
    if parsed.fragment or not parsed.hostname:
        return False
    if parsed.scheme == "https" and parsed.hostname in settings.VAULT_OAUTH_REDIRECT_HOSTS:
        return True
    return settings.VAULT_OAUTH_ALLOW_LOOPBACK and parsed.scheme == "http" and parsed.hostname in LOOPBACK_HOSTS


def _redirect_matches(registered, given):
    if registered == given:
        return True
    # RFC 8252 7.3: loopback redirects match regardless of port.
    r, g = urlparse(registered), urlparse(given)
    return (
        r.scheme == g.scheme == "http"
        and r.hostname == g.hostname
        and r.hostname in LOOPBACK_HOSTS
        and r.path == g.path
        and r.query == g.query
    )


def add_query(uri, params):
    parts = urlparse(uri)
    query = parse_qsl(parts.query, keep_blank_values=True) + [(k, v) for k, v in params.items() if v is not None]
    return urlunparse(parts._replace(query=urlencode(query)))


def granted_scope_string(scopes):
    return " ".join(s for s in SCOPES if s in scopes)


# --- Registration (RFC 7591) ------------------------------------------------


def register_client(data):
    if not isinstance(data, dict):
        raise OAuthError("invalid_client_metadata", "body must be a JSON object")
    redirect_uris = data.get("redirect_uris")
    if not isinstance(redirect_uris, list) or not 1 <= len(redirect_uris) <= 10:
        raise OAuthError("invalid_redirect_uri", "redirect_uris must list 1-10 URIs")
    for uri in redirect_uris:
        if not redirect_uri_allowed(uri):
            raise OAuthError("invalid_redirect_uri", f"redirect URI not allowed: {str(uri)[:200]}")
    grant_types = data.get("grant_types") or ["authorization_code", "refresh_token"]
    if not isinstance(grant_types, list) or not set(grant_types) <= {"authorization_code", "refresh_token"}:
        raise OAuthError("invalid_client_metadata", "only authorization_code and refresh_token grants are supported")
    response_types = data.get("response_types") or ["code"]
    if response_types != ["code"]:
        raise OAuthError("invalid_client_metadata", "only response_type=code is supported")
    name = data.get("client_name") if isinstance(data.get("client_name"), str) else ""

    client = OAuthClient.objects.create(
        client_id=secrets.token_urlsafe(24), client_name=name.strip()[:100], redirect_uris=redirect_uris
    )
    # Always a public client: PKCE instead of a client secret.
    return {
        "client_id": client.client_id,
        "client_id_issued_at": int(client.created_at.timestamp()),
        "client_name": client.client_name,
        "redirect_uris": redirect_uris,
        "grant_types": grant_types,
        "response_types": ["code"],
        "token_endpoint_auth_method": "none",
    }


# --- Authorization ----------------------------------------------------------


@dataclass
class AuthRequest:
    client: OAuthClient
    redirect_uri: str
    state: str
    code_challenge: str
    requested_scopes: tuple
    resource: str


def parse_authorization_request(params, request):
    client = OAuthClient.objects.filter(client_id=params.get("client_id") or "").first()
    if client is None:
        raise OAuthError("invalid_client", "Unknown client. Remove the connector and add it again.")
    redirect_uri = params.get("redirect_uri") or (client.redirect_uris[0] if len(client.redirect_uris) == 1 else "")
    if not any(_redirect_matches(r, redirect_uri) for r in client.redirect_uris) or not redirect_uri_allowed(redirect_uri):
        raise OAuthError("invalid_request", "redirect_uri does not match the registered client.")

    def fail(error, description):
        return OAuthError(error, description, redirectable=True)

    if params.get("response_type") != "code":
        raise fail("unsupported_response_type", "response_type must be code")
    challenge = params.get("code_challenge") or ""
    if params.get("code_challenge_method") != "S256" or not 43 <= len(challenge) <= 128:
        raise fail("invalid_request", "PKCE with code_challenge_method=S256 is required")
    resource = params.get("resource") or mcp_resource(request)
    if resource.rstrip("/") != mcp_resource(request):
        raise fail("invalid_target", "unknown resource")
    requested = tuple(s for s in (params.get("scope") or "").split() if s in SCOPES) or SCOPES
    return AuthRequest(
        client=client,
        redirect_uri=redirect_uri,
        state=params.get("state") or "",
        code_challenge=challenge,
        requested_scopes=requested,
        resource=mcp_resource(request),
    )


def login_locked():
    since = timezone.now() - FAILED_LOGIN_WINDOW
    failures = AccessLog.objects.filter(action="oauth_login", success=False, timestamp__gte=since).count()
    return failures >= settings.VAULT_OAUTH_MAX_FAILED_LOGINS


def owner_password_configured():
    return bool(settings.VAULT_OWNER_PASSWORD_HASH)


def check_owner_password(password):
    return owner_password_configured() and isinstance(password, str) and check_password(
        password, settings.VAULT_OWNER_PASSWORD_HASH
    )


def issue_code(auth_request, scopes):
    code = _new_secret()
    OAuthCode.objects.create(
        code_hash=_hash(code),
        client=auth_request.client,
        redirect_uri=auth_request.redirect_uri,
        code_challenge=auth_request.code_challenge,
        scope=granted_scope_string(scopes),
        resource=auth_request.resource,
        expires_at=timezone.now() + CODE_LIFETIME,
    )
    return code


# --- Token endpoint ---------------------------------------------------------


def _pkce_ok(verifier, challenge):
    if not isinstance(verifier, str) or not 43 <= len(verifier) <= 128:
        return False
    digest = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("ascii", "replace")).digest()).rstrip(b"=")
    return secrets.compare_digest(digest.decode("ascii"), challenge)


def _issue_tokens(client, scope, resource):
    access, refresh = _new_secret(), _new_secret()
    now = timezone.now()
    OAuthToken.objects.create(
        access_hash=_hash(access),
        refresh_hash=_hash(refresh),
        client=client,
        scope=scope,
        resource=resource,
        access_expires_at=now + timedelta(seconds=settings.VAULT_OAUTH_ACCESS_TOKEN_SECONDS),
        refresh_expires_at=now + timedelta(days=settings.VAULT_OAUTH_REFRESH_TOKEN_DAYS),
    )
    return {
        "access_token": access,
        "token_type": "Bearer",
        "expires_in": settings.VAULT_OAUTH_ACCESS_TOKEN_SECONDS,
        "refresh_token": refresh,
        "scope": scope,
    }


def exchange_code(form, request):
    code = form.get("code") or ""
    with transaction.atomic():
        row = OAuthCode.objects.select_for_update().select_related("client").filter(code_hash=_hash(code)).first()
        if row is None or row.used or row.expires_at <= timezone.now():
            raise OAuthError("invalid_grant", "authorization code is invalid, expired or already used")
        row.used = True
        row.save(update_fields=["used"])
    if form.get("client_id") != row.client.client_id:
        raise OAuthError("invalid_grant", "client_id does not match the authorization code")
    if form.get("redirect_uri") and not _redirect_matches(row.redirect_uri, form.get("redirect_uri")):
        raise OAuthError("invalid_grant", "redirect_uri does not match the authorization request")
    if not _pkce_ok(form.get("code_verifier"), row.code_challenge):
        raise OAuthError("invalid_grant", "PKCE verification failed")
    if form.get("resource") and form.get("resource").rstrip("/") != row.resource:
        raise OAuthError("invalid_target", "unknown resource")
    return _issue_tokens(row.client, row.scope, row.resource)


def refresh_tokens(form):
    refresh = form.get("refresh_token") or ""
    with transaction.atomic():
        row = (
            OAuthToken.objects.select_for_update()
            .select_related("client")
            .filter(refresh_hash=_hash(refresh), revoked=False)
            .first()
        )
        if row is None or row.refresh_expires_at <= timezone.now():
            raise OAuthError("invalid_grant", "refresh token is invalid or expired")
        if form.get("client_id") and form.get("client_id") != row.client.client_id:
            raise OAuthError("invalid_grant", "client_id does not match the refresh token")
        row.revoked = True  # rotation: the old pair stops working now
        row.save(update_fields=["revoked"])
        return _issue_tokens(row.client, row.scope, row.resource)


def token_response(form, request):
    grant_type = form.get("grant_type")
    if grant_type == "authorization_code":
        return exchange_code(form, request)
    if grant_type == "refresh_token":
        return refresh_tokens(form)
    raise OAuthError("unsupported_grant_type", "use authorization_code or refresh_token")


# --- Resource server side ---------------------------------------------------


def authenticate_access_token(token, request):
    """Return the live OAuthToken for ``token`` issued for this MCP server, or None."""
    row = OAuthToken.objects.select_related("client").filter(access_hash=_hash(token), revoked=False).first()
    now = timezone.now()
    if row is None or row.access_expires_at <= now or row.resource != mcp_resource(request):
        return None
    if row.last_used_at is None or now - row.last_used_at > timedelta(minutes=5):
        OAuthToken.objects.filter(pk=row.pk).update(last_used_at=now)
    return row
