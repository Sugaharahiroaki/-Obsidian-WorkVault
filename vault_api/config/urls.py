from django.urls import include, path

urlpatterns = [
    path("api/vault/", include("vault.urls")),
]

handler404 = "vault.views.not_found"
handler500 = "vault.views.server_error"
