"""Disconnect ChatGPT / Claude: revoke OAuth tokens (e.g. after losing a phone).

    python manage.py vault_revoke_tokens              # all connections
    python manage.py vault_revoke_tokens --list
"""
from django.core.management.base import BaseCommand
from django.utils import timezone

from vault.models import OAuthToken


class Command(BaseCommand):
    help = "Revoke OAuth tokens so MCP clients must sign in again."

    def add_arguments(self, parser):
        parser.add_argument("--list", action="store_true", help="only list active connections")

    def handle(self, *args, **options):
        active = OAuthToken.objects.filter(revoked=False, refresh_expires_at__gt=timezone.now()).select_related("client")
        if options["list"]:
            for token in active.order_by("-created_at"):
                name = token.client.client_name or token.client.client_id[:8]
                issued = f"{timezone.localtime(token.created_at):%Y-%m-%d %H:%M}"
                last = f"{timezone.localtime(token.last_used_at):%Y-%m-%d %H:%M}" if token.last_used_at else "-"
                self.stdout.write(f"{name:30} scope={token.scope:24} issued={issued} last_used={last}")
            return
        count = active.update(revoked=True)
        self.stdout.write(f"Revoked {count} token(s). Connected apps must sign in again.")
