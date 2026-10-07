"""Access log (timestamp, action, path, client, success, IP).

Only metadata is recorded: never note bodies, search queries or API keys.
"""
import ipaddress

from django.conf import settings

from ..models import AccessLog


def client_ip(request):
    """Client IP, trusting X-Forwarded-For only for configured proxies."""
    proxies = settings.VAULT_TRUSTED_PROXY_COUNT
    if proxies > 0:
        forwarded = [p.strip() for p in request.META.get("HTTP_X_FORWARDED_FOR", "").split(",") if p.strip()]
        if len(forwarded) >= proxies:
            # Each trusted proxy appends the address it saw; take the one
            # added by the outermost trusted proxy.
            ip = _valid_ip(forwarded[-proxies])
            if ip:
                return ip
    return _valid_ip(request.META.get("REMOTE_ADDR", ""))


def _valid_ip(value):
    try:
        return str(ipaddress.ip_address(value))
    except ValueError:
        return None


def _entry(request, action, *, success, path="", client="", detail=""):
    return AccessLog(
        action=action,
        path=(path or "")[:1024],
        client=(client or "")[:64],
        success=success,
        ip=client_ip(request),
        detail=(detail or "")[:255],
    )


def record(request, action, *, success, path="", client="", detail=""):
    _entry(request, action, success=success, path=path, client=client, detail=detail).save()


def record_many(request, action, entries, *, client):
    """entries: iterable of (path, success, detail)."""
    AccessLog.objects.bulk_create(
        [
            _entry(request, action, success=success, path=path, client=client, detail=detail)
            for path, success, detail in entries
        ]
    )
