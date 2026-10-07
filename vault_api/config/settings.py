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
    # API views are csrf_exempt (Bearer auth, no cookies); kept for anything added later.
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
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
# 32 characters. Sync keys (Windows client) push notes and pull AI writes;
# write keys (e.g. ChatGPT) read and create/append/update; read keys only read.
VAULT_SYNC_KEYS = os.environ.get("VAULT_SYNC_KEYS", "")
VAULT_WRITE_KEYS = os.environ.get("VAULT_WRITE_KEYS", "")
VAULT_READ_KEYS = os.environ.get("VAULT_READ_KEYS", "")

# Public base URL, e.g. https://vault.example.com. Used as the OAuth issuer and
# MCP resource identifier, and in the OpenAPI schema. Empty = derive from the
# request (fine for local testing; set it in production).
VAULT_PUBLIC_URL = os.environ.get("VAULT_PUBLIC_URL", "").rstrip("/")
# Obsidian vault name, used for obsidian:// citation links in MCP search results.
VAULT_OBSIDIAN_VAULT_NAME = os.environ.get("VAULT_OBSIDIAN_VAULT_NAME", "WorkVault")

# --- OAuth for MCP clients (ChatGPT / Claude apps) ------------------------
# Hash of the owner's password, entered on the consent screen when ChatGPT or
# Claude connects. Create it with: python manage.py vault_owner_password
# Empty = OAuth sign-in disabled (static API keys still work).
VAULT_OWNER_PASSWORD_HASH = os.environ.get("VAULT_OWNER_PASSWORD_HASH", "")
# Hosts allowed as OAuth redirect targets (plus loopback for Claude Code).
VAULT_OAUTH_REDIRECT_HOSTS = env_list("VAULT_OAUTH_REDIRECT_HOSTS", "chatgpt.com,claude.ai")
VAULT_OAUTH_ALLOW_LOOPBACK = env_bool("VAULT_OAUTH_ALLOW_LOOPBACK", True)
VAULT_OAUTH_ACCESS_TOKEN_SECONDS = env_int("VAULT_OAUTH_ACCESS_TOKEN_SECONDS", 3600)
VAULT_OAUTH_REFRESH_TOKEN_DAYS = env_int("VAULT_OAUTH_REFRESH_TOKEN_DAYS", 60)
# Failed password attempts allowed per 15 minutes (all clients together).
VAULT_OAUTH_MAX_FAILED_LOGINS = env_int("VAULT_OAUTH_MAX_FAILED_LOGINS", 10)

CSRF_COOKIE_SECURE = VAULT_REQUIRE_HTTPS
CSRF_COOKIE_HTTPONLY = True

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
