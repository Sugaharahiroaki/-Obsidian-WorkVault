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
