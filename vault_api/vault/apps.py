import logging

from django.apps import AppConfig

logger = logging.getLogger("vault")


class VaultConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "vault"
    verbose_name = "Obsidian Vault"

    def ready(self):
        from . import auth

        # Fail fast on malformed key configuration (raises ImproperlyConfigured).
        if not auth.load_api_keys():
            logger.warning("No API keys configured; every API request will be rejected.")
