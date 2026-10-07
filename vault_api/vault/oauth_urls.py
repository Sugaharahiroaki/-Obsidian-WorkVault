"""OAuth endpoints for MCP clients. Include at the site root:

    path("", include("vault.oauth_urls")),

The well-known paths must live at the root of the host (RFC 8414 / RFC 9728).
"""
from django.urls import path, re_path

from . import oauth_views

app_name = "vault-oauth"

urlpatterns = [
    # RFC 9728: /.well-known/oauth-protected-resource[/<mcp path>]
    re_path(r"^\.well-known/oauth-protected-resource(?:/.*)?$", oauth_views.protected_resource_metadata, name="resource-metadata"),
    re_path(r"^\.well-known/oauth-authorization-server(?:/.*)?$", oauth_views.authorization_server_metadata, name="server-metadata"),
    path("oauth/register", oauth_views.register, name="register"),
    path("oauth/authorize", oauth_views.authorize, name="authorize"),
    path("oauth/token", oauth_views.token, name="token"),
]
