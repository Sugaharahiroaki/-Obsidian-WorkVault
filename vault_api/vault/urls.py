from django.urls import path

from . import views

app_name = "vault"

urlpatterns = [
    path("sync/", views.sync_view, name="sync"),
    path("search/", views.search_view, name="search"),
    path("note/", views.note_view, name="note"),
    path("tree/", views.tree_view, name="tree"),
    path("recent/", views.recent_view, name="recent"),
]
