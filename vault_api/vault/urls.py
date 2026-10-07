from django.urls import path

from . import mcp, views

app_name = "vault"

urlpatterns = [
    # Windows sync client
    path("sync/", views.sync_view, name="sync"),
    path("changes/", views.changes_view, name="changes"),
    path("changes/ack/", views.changes_ack_view, name="changes-ack"),
    # Read
    path("search/", views.search_view, name="search"),
    path("note/", views.note_view, name="note"),
    path("list/", views.list_view, name="list"),
    path("tree/", views.tree_view, name="tree"),
    path("recent/", views.recent_view, name="recent"),
    # Write (Phase 3)
    path("note/create/", views.create_view, name="create"),
    path("note/append/", views.append_view, name="append"),
    path("note/update/", views.update_view, name="update"),
    # MCP for ChatGPT / Claude apps, Claude Code, Codex (no trailing slash: clients POST to it as entered)
    path("mcp", mcp.mcp_view, name="mcp"),
    # OpenAPI schema (for HTTP tools; not needed for MCP)
    path("openapi.json", views.openapi_view, name="openapi"),
]
