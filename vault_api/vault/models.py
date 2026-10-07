from django.db import models


class VaultNote(models.Model):
    """A Markdown note mirrored from the Windows WorkVault.

    ``content`` is stored verbatim, so frontmatter, [[wiki links]], ![[embeds]]
    and #tags survive untouched. ``created_at``/``updated_at`` are the file's
    own timestamps as reported by the sync client; ``last_synced_at`` is when
    the server last accepted the note.
    """

    path = models.CharField(max_length=1024, unique=True)
    title = models.CharField(max_length=255)
    content = models.TextField(blank=True)
    folder = models.CharField(max_length=1024, blank=True, db_index=True)
    content_hash = models.CharField(max_length=64)
    created_at = models.DateTimeField()
    updated_at = models.DateTimeField(db_index=True)
    last_synced_at = models.DateTimeField()

    class Meta:
        ordering = ["path"]

    def __str__(self):
        return self.path


class PendingChange(models.Model):
    """A write requested by an AI client, waiting for the Windows client to apply it.

    The server never touches the PC. The sync client pulls pending changes,
    applies each one only if the file on the PC is still in the expected state
    (otherwise it saves a conflict copy) and acknowledges the result.
    """

    OP_CREATE = "create"
    OP_APPEND = "append"
    OP_UPDATE = "update"
    OP_CHOICES = [(OP_CREATE, "create"), (OP_APPEND, "append"), (OP_UPDATE, "update")]

    PENDING = "pending"
    APPLIED = "applied"
    CONFLICT = "conflict"
    FAILED = "failed"
    STATUS_CHOICES = [(PENDING, "pending"), (APPLIED, "applied"), (CONFLICT, "conflict"), (FAILED, "failed")]

    op = models.CharField(max_length=10, choices=OP_CHOICES)
    path = models.CharField(max_length=1024, db_index=True)
    # Full new content for create/update; the text to add for append.
    content = models.TextField()
    # Hash the note had when the change was accepted (update/append).
    base_hash = models.CharField(max_length=64, blank=True)
    status = models.CharField(max_length=10, choices=STATUS_CHOICES, default=PENDING, db_index=True)
    client = models.CharField(max_length=64)
    created_at = models.DateTimeField(auto_now_add=True)
    applied_at = models.DateTimeField(null=True, blank=True)
    conflict_path = models.CharField(max_length=1024, blank=True)
    detail = models.CharField(max_length=255, blank=True)

    class Meta:
        ordering = ["id"]

    def __str__(self):
        return f"#{self.pk} {self.op} {self.path} ({self.status})"


class OAuthClient(models.Model):
    """An MCP client (ChatGPT, Claude, ...) registered via Dynamic Client Registration.

    Public clients only (PKCE, no client secret). Registering grants nothing:
    access still needs the owner's password on the consent screen.
    """

    client_id = models.CharField(max_length=64, unique=True)
    client_name = models.CharField(max_length=100, blank=True)
    redirect_uris = models.JSONField(default=list)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"{self.client_name or 'client'} ({self.client_id[:8]}…)"


class OAuthCode(models.Model):
    """Single-use authorization code (stored hashed, valid for a few minutes)."""

    code_hash = models.CharField(max_length=64, unique=True)
    client = models.ForeignKey(OAuthClient, on_delete=models.CASCADE)
    redirect_uri = models.CharField(max_length=1024)
    code_challenge = models.CharField(max_length=128)
    scope = models.CharField(max_length=200)
    resource = models.CharField(max_length=1024)
    expires_at = models.DateTimeField()
    used = models.BooleanField(default=False)


class OAuthToken(models.Model):
    """Access + refresh token pair (stored hashed). Refresh rotates the pair."""

    access_hash = models.CharField(max_length=64, unique=True)
    refresh_hash = models.CharField(max_length=64, unique=True)
    client = models.ForeignKey(OAuthClient, on_delete=models.CASCADE)
    scope = models.CharField(max_length=200)
    resource = models.CharField(max_length=1024)
    access_expires_at = models.DateTimeField()
    refresh_expires_at = models.DateTimeField()
    revoked = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)
    last_used_at = models.DateTimeField(null=True, blank=True)


class AccessLog(models.Model):
    """Audit trail of API calls. Never stores note bodies, queries or keys."""

    timestamp = models.DateTimeField(auto_now_add=True, db_index=True)
    action = models.CharField(max_length=32)
    path = models.CharField(max_length=1024, blank=True)
    client = models.CharField(max_length=64, blank=True)
    success = models.BooleanField()
    ip = models.GenericIPAddressField(null=True, blank=True)
    detail = models.CharField(max_length=255, blank=True)

    class Meta:
        ordering = ["-timestamp"]

    def __str__(self):
        return f"{self.timestamp:%Y-%m-%d %H:%M:%S} {self.action} {self.client} {self.success}"
