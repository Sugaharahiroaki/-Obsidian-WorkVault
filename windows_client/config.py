"""Configuration for the WorkVault sync client.

Values come from windows_client/.env (or the file named by VAULT_CLIENT_ENV)
and the process environment. The API key is never included in reprs or logs.
"""
import os
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlparse

from dotenv import load_dotenv

CLIENT_DIR = Path(__file__).resolve().parent
DEFAULT_VAULT_ROOT = r"C:\Users\hsuga\Documents\Obsidian\WorkVault"
DEFAULT_DATA_DIR = Path(os.environ.get("LOCALAPPDATA") or Path.home()) / "obsidian-vault-bridge"
LOOPBACK_HOSTS = {"localhost", "127.0.0.1", "::1"}


class ConfigError(Exception):
    pass


@dataclass(frozen=True)
class Config:
    vault_root: Path
    api_url: str
    api_key: str = field(repr=False)
    state_file: Path = DEFAULT_DATA_DIR / "state.json"
    log_file: Path = DEFAULT_DATA_DIR / "sync.log"
    debounce_seconds: float = 2.5
    rescan_interval_seconds: float = 600.0
    retry_interval_seconds: float = 60.0
    batch_max_notes: int = 50
    batch_max_bytes: int = 4 * 1024 * 1024
    max_note_bytes: int = 2 * 1024 * 1024
    request_timeout: float = 30.0
    secret_scan: bool = True
    secret_scan_allow: tuple = ()
    extra_excludes: tuple = ()
    ca_bundle: str = ""
    # Phase 3: apply notes created/edited by AI clients to the vault.
    apply_remote_changes: bool = True
    pull_interval_seconds: float = 30.0

    def endpoint(self, name):
        return f"{self.api_url.rstrip('/')}/{name}/"


def _bool(value, default):
    if value is None or value.strip() == "":
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _globs(value):
    return tuple(p.strip().replace("\\", "/") for p in (value or "").split(",") if p.strip())


def _number(env, name, default, kind=float):
    value = env.get(name)
    if value is None or value.strip() == "":
        return default
    try:
        return kind(value)
    except ValueError:
        raise ConfigError(f"{name} must be a number") from None


def validate_api_url(url):
    """HTTPS only; plain HTTP is allowed solely for a server on this machine."""
    parsed = urlparse(url)
    if parsed.scheme == "https" and parsed.hostname:
        return
    if parsed.scheme == "http" and parsed.hostname in LOOPBACK_HOSTS:
        return
    raise ConfigError("VAULT_API_URL must be an https:// URL (http:// is allowed only for localhost).")


def load_config(env_file=None, require_api=True, environ=None):
    if environ is None:
        load_dotenv(env_file or os.environ.get("VAULT_CLIENT_ENV") or CLIENT_DIR / ".env")
        environ = os.environ
    env = environ

    vault_root = Path(env.get("VAULT_ROOT") or DEFAULT_VAULT_ROOT)
    if not vault_root.is_dir():
        raise ConfigError(f"VAULT_ROOT does not exist or is not a folder: {vault_root}")

    api_url = (env.get("VAULT_API_URL") or "").strip()
    api_key = (env.get("VAULT_API_KEY") or "").strip()
    if require_api:
        if not api_url:
            raise ConfigError("VAULT_API_URL is not set.")
        validate_api_url(api_url)
        if len(api_key) < 32:
            raise ConfigError("VAULT_API_KEY is missing or shorter than 32 characters.")

    data_dir = Path(env.get("VAULT_DATA_DIR") or DEFAULT_DATA_DIR)
    return Config(
        vault_root=vault_root.resolve(),
        api_url=api_url,
        api_key=api_key,
        state_file=Path(env.get("VAULT_STATE_FILE") or data_dir / "state.json"),
        log_file=Path(env.get("VAULT_LOG_FILE") or data_dir / "sync.log"),
        debounce_seconds=_number(env, "VAULT_DEBOUNCE_SECONDS", 2.5),
        rescan_interval_seconds=_number(env, "VAULT_RESCAN_INTERVAL_SECONDS", 600.0),
        batch_max_notes=_number(env, "VAULT_BATCH_MAX_NOTES", 50, int),
        batch_max_bytes=_number(env, "VAULT_BATCH_MAX_BYTES", 4 * 1024 * 1024, int),
        max_note_bytes=_number(env, "VAULT_MAX_NOTE_BYTES", 2 * 1024 * 1024, int),
        request_timeout=_number(env, "VAULT_REQUEST_TIMEOUT", 30.0),
        secret_scan=_bool(env.get("VAULT_SECRET_SCAN"), True),
        secret_scan_allow=_globs(env.get("VAULT_SECRET_SCAN_ALLOW")),
        extra_excludes=_globs(env.get("VAULT_EXTRA_EXCLUDES")),
        ca_bundle=(env.get("VAULT_CA_BUNDLE") or "").strip(),
        apply_remote_changes=_bool(env.get("VAULT_APPLY_REMOTE_CHANGES"), True),
        pull_interval_seconds=_number(env, "VAULT_PULL_INTERVAL_SECONDS", 30.0),
    )
