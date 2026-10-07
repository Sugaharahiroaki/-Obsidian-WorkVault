"""Settings for the test suite: python manage.py test --settings=config.test_settings"""
import os

os.environ.setdefault("DJANGO_SECRET_KEY", "test-only-secret-key")

from .settings import *  # noqa: E402,F401,F403

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": str(BASE_DIR / "test-db.sqlite3"),  # noqa: F405
    }
}
ALLOWED_HOSTS = ["testserver", "localhost"]
VAULT_REQUIRE_HTTPS = False
VAULT_TRUSTED_PROXY_COUNT = 0
VAULT_SYNC_KEYS = "test-pc:" + "s" * 40
VAULT_READ_KEYS = "test-reader:" + "r" * 40
LOGGING["loggers"]["django"]["level"] = "ERROR"  # noqa: F405 - silence expected 4xx warnings
