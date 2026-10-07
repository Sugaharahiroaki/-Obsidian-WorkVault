"""Django settings for the Vault API.

Every environment-specific value comes from environment variables, optionally
loaded from ``vault_api/.env`` (see ``.env.example``). Defaults are the safe
production choice: DEBUG off and HTTPS required.
"""
import os
from pathlib import Path

from django.core.exceptions import ImproperlyConfigured
from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / ".env")


def env_bool(name, default=False):
    value = os.environ.get(name)
    if value is None or value.strip() == "":
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def env_int(name, default):
    value = os.environ.get(name)
    if value is None or value.strip() == "":
        return default
    return int(value)


def env_list(name, default=""):
    return [item.strip() for item in os.environ.get(name, default).split(",") if item.strip()]


DEBUG = env_bool("DJANGO_DEBUG", False)

SECRET_KEY = os.environ.get("DJANGO_SECRET_KEY", "")
if not SECRET_KEY:
    if not DEBUG:
        raise ImproperlyConfigured("DJANGO_SECRET_KEY must be set when DJANGO_DEBUG is false.")
    SECRET_KEY = "insecure-development-only-key"

ALLOWED_HOSTS = env_list("DJANGO_ALLOWED_HOSTS", "localhost,127.0.0.1" if DEBUG else "")

INSTALLED_APPS = [
    "vault",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "django.middleware.common.CommonMiddleware",
]

ROOT_URLCONF = "config.urls"
WSGI_APPLICATION = "config.wsgi.application"
TEMPLATES = []

# --- Database --------------------------------------------------------------
# SQLite is the default and enables FTS5 search. PostgreSQL also works; search
# then uses case-insensitive LIKE only.
DB_ENGINE = os.environ.get("VAULT_DB_ENGINE", "sqlite").strip().lower()
if DB_ENGINE == "sqlite":
    DATABASES = {
        "default": {
            "ENGINE": "django.db.backends.sqlite3",
            "NAME": os.environ.get("VAULT_DB_PATH") or str(BASE_DIR / "db.sqlite3"),
            "OPTIONS": {
                "timeout": 20,
                "init_command": "PRAGMA journal_mode=WAL; PRAGMA synchronous=NORMAL;",
            },
        }
    }
elif DB_ENGINE == "postgresql":
    DATABASES = {
        "default": {
            "ENGINE": "django.db.backends.postgresql",
            "NAME": os.environ.get("VAULT_DB_NAME", "vault_api"),
            "USER": os.environ.get("VAULT_DB_USER", ""),
            "PASSWORD": os.environ.get("VAULT_DB_PASSWORD", ""),
            "HOST": os.environ.get("VAULT_DB_HOST", ""),
            "PORT": os.environ.get("VAULT_DB_PORT", ""),
        }
    }
else:
    raise ImproperlyConfigured("VAULT_DB_ENGINE must be 'sqlite' or 'postgresql'.")

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

LANGUAGE_CODE = "ja"
TIME_ZONE = os.environ.get("DJANGO_TIME_ZONE", "Asia/Tokyo")
USE_I18N = False
USE_TZ = True

# --- HTTPS -----------------------------------------------------------------
# Reject plain-HTTP API calls (the API key would travel in clear text).
VAULT_REQUIRE_HTTPS = env_bool("VAULT_REQUIRE_HTTPS", True)
# Behind nginx/Caddy that terminates TLS and sets X-Forwarded-Proto.
if env_bool("VAULT_SECURE_PROXY_SSL_HEADER", False):
    SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
SECURE_SSL_REDIRECT = env_bool("DJANGO_SECURE_SSL_REDIRECT", False)
SECURE_HSTS_SECONDS = env_int("DJANGO_SECURE_HSTS_SECONDS", 0)
SECURE_CONTENT_TYPE_NOSNIFF = True
SECURE_REFERRER_POLICY = "no-referrer"

# --- Vault API -------------------------------------------------------------
# API keys as "client-name:key" pairs, comma separated. Keys must be at least
# 32 characters. Sync keys may push notes and read; read keys may only read.
VAULT_SYNC_KEYS = os.environ.get("VAULT_SYNC_KEYS", "")
VAULT_READ_KEYS = os.environ.get("VAULT_READ_KEYS", "")

# Number of reverse proxies in front of Django whose X-Forwarded-For entries
# are trusted for the client IP in access logs. 0 = use REMOTE_ADDR.
VAULT_TRUSTED_PROXY_COUNT = env_int("VAULT_TRUSTED_PROXY_COUNT", 0)

VAULT_MAX_NOTE_BYTES = env_int("VAULT_MAX_NOTE_BYTES", 2 * 1024 * 1024)
VAULT_MAX_BATCH_NOTES = env_int("VAULT_MAX_BATCH_NOTES", 100)
DATA_UPLOAD_MAX_MEMORY_SIZE = env_int("VAULT_MAX_REQUEST_BYTES", 10 * 1024 * 1024)

# --- Logging ---------------------------------------------------------------
# The audit trail lives in the AccessLog table. Process logs never contain
# note bodies, search queries or API keys.
LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "plain": {"format": "%(asctime)s %(levelname)s %(name)s %(message)s"},
    },
    "handlers": {
        "console": {"class": "logging.StreamHandler", "formatter": "plain"},
    },
    "loggers": {
        "vault": {
            "handlers": ["console"],
            "level": os.environ.get("VAULT_LOG_LEVEL", "INFO"),
            "propagate": False,
        },
        "django": {"handlers": ["console"], "level": "WARNING"},
    },
}
