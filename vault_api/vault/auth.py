"""Bearer API-key authentication with per-key scopes.

Keys live in the environment (``.env``), never in code or the database::

    VAULT_SYNC_KEYS=windows-pc:<key>            # push notes + read
    VAULT_READ_KEYS=chatgpt:<key>,codex:<key>   # read only

Keys are compared in constant time and are never logged or echoed in errors.
"""
import hashlib
import hmac
import re
from dataclasses import dataclass

from django.conf import settings
from django.core.exceptions import ImproperlyConfigured

SCOPE_READ = "read"
SCOPE_SYNC = "sync"
MIN_KEY_LENGTH = 32

_CLIENT_NAME = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")


@dataclass(frozen=True)
class ApiKey:
    client: str
    digest: bytes
    scopes: frozenset

    def __repr__(self):
        return f"ApiKey(client={self.client!r}, scopes={sorted(self.scopes)})"


def _digest(token):
    return hashlib.sha256(token.encode("utf-8")).digest()


def _parse(setting_name, raw, scopes):
    keys = []
    for index, entry in enumerate((raw or "").split(","), start=1):
        entry = entry.strip()
        if not entry:
            continue
        client, sep, token = entry.partition(":")
        if (
            not sep
            or not _CLIENT_NAME.match(client)
            or len(token) < MIN_KEY_LENGTH
            or any(ch.isspace() for ch in token)
        ):
            # Report the position only: the entry itself may be a bare key.
            raise ImproperlyConfigured(
                f"Entry #{index} of {setting_name} is malformed; expected "
                f"'client-name:key' with a key of at least {MIN_KEY_LENGTH} characters."
            )
        keys.append(ApiKey(client=client, digest=_digest(token), scopes=frozenset(scopes)))
    return keys


def load_api_keys():
    return _parse("VAULT_SYNC_KEYS", settings.VAULT_SYNC_KEYS, {SCOPE_SYNC, SCOPE_READ}) + _parse(
        "VAULT_READ_KEYS", settings.VAULT_READ_KEYS, {SCOPE_READ}
    )


def authenticate(request):
    """Return the matching ApiKey for ``Authorization: Bearer <key>``, or None."""
    scheme, _, token = request.headers.get("Authorization", "").partition(" ")
    token = token.strip()
    if scheme.lower() != "bearer" or not token:
        return None
    digest = _digest(token)
    match = None
    # Compare against every key so timing does not reveal which one matched.
    for key in load_api_keys():
        if hmac.compare_digest(key.digest, digest) and match is None:
            match = key
    return match
