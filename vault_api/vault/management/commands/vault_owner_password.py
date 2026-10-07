"""Create the VAULT_OWNER_PASSWORD_HASH line for .env.

    python manage.py vault_owner_password            # asks twice
    printf '%s' "$PW" | python manage.py vault_owner_password --stdin
"""
import getpass
import sys

from django.contrib.auth.hashers import make_password
from django.core.management.base import BaseCommand, CommandError

MIN_LENGTH = 12


class Command(BaseCommand):
    help = "Print the VAULT_OWNER_PASSWORD_HASH line for .env (the password itself is never stored)."

    def add_arguments(self, parser):
        parser.add_argument("--stdin", action="store_true", help="read the password from standard input")

    def handle(self, *args, **options):
        if options["stdin"]:
            password = sys.stdin.read().rstrip("\r\n")
        else:
            password = getpass.getpass("Owner password: ")
            if password != getpass.getpass("Again: "):
                raise CommandError("Passwords do not match.")
        if len(password) < MIN_LENGTH:
            raise CommandError(f"Use at least {MIN_LENGTH} characters.")
        # Single quotes keep python-dotenv from interpreting the "$" separators.
        self.stdout.write(f"VAULT_OWNER_PASSWORD_HASH='{make_password(password)}'")
