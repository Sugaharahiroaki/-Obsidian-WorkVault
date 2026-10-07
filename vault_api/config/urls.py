from django.urls import include, path

urlpatterns = [
    path("api/vault/", include("vault.urls")),
    path("", include("vault.oauth_urls")),
]

handler404 = "vault.views.not_found"
handler500 = "vault.views.server_error"
