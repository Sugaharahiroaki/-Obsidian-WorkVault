"""SQLite FTS5 index over VaultNote (title, path, content).

Uses the trigram tokenizer (SQLite >= 3.34) so Japanese text is searchable
without a word segmenter. Triggers keep the index in step with the table.
On other databases, or SQLite builds without FTS5/trigram, nothing is
created and search falls back to LIKE.
"""
from django.db import DatabaseError, migrations, transaction

FTS_TABLE = "vault_note_fts"

CREATE_STATEMENTS = [
    f"""
    CREATE VIRTUAL TABLE IF NOT EXISTS {FTS_TABLE} USING fts5(
        title, path, content,
        content='vault_vaultnote', content_rowid='id',
        tokenize='trigram'
    )
    """,
    f"""
    CREATE TRIGGER IF NOT EXISTS vault_note_fts_ai AFTER INSERT ON vault_vaultnote BEGIN
        INSERT INTO {FTS_TABLE}(rowid, title, path, content)
        VALUES (new.id, new.title, new.path, new.content);
    END
    """,
    f"""
    CREATE TRIGGER IF NOT EXISTS vault_note_fts_ad AFTER DELETE ON vault_vaultnote BEGIN
        INSERT INTO {FTS_TABLE}({FTS_TABLE}, rowid, title, path, content)
        VALUES ('delete', old.id, old.title, old.path, old.content);
    END
    """,
    f"""
    CREATE TRIGGER IF NOT EXISTS vault_note_fts_au AFTER UPDATE OF title, path, content ON vault_vaultnote BEGIN
        INSERT INTO {FTS_TABLE}({FTS_TABLE}, rowid, title, path, content)
        VALUES ('delete', old.id, old.title, old.path, old.content);
        INSERT INTO {FTS_TABLE}(rowid, title, path, content)
        VALUES (new.id, new.title, new.path, new.content);
    END
    """,
    f"INSERT INTO {FTS_TABLE}({FTS_TABLE}) VALUES ('rebuild')",
]

DROP_STATEMENTS = [
    "DROP TRIGGER IF EXISTS vault_note_fts_au",
    "DROP TRIGGER IF EXISTS vault_note_fts_ad",
    "DROP TRIGGER IF EXISTS vault_note_fts_ai",
    f"DROP TABLE IF EXISTS {FTS_TABLE}",
]


def _trigram_supported(connection):
    try:
        with transaction.atomic(using=connection.alias), connection.cursor() as cursor:
            cursor.execute("CREATE VIRTUAL TABLE temp.vault_fts_probe USING fts5(x, tokenize='trigram')")
            cursor.execute("DROP TABLE temp.vault_fts_probe")
        return True
    except DatabaseError:
        return False


def create_fts(apps, schema_editor):
    connection = schema_editor.connection
    if connection.vendor != "sqlite" or not _trigram_supported(connection):
        return
    with connection.cursor() as cursor:
        for statement in CREATE_STATEMENTS:
            cursor.execute(statement)


def drop_fts(apps, schema_editor):
    connection = schema_editor.connection
    if connection.vendor != "sqlite":
        return
    with connection.cursor() as cursor:
        for statement in DROP_STATEMENTS:
            cursor.execute(statement)


class Migration(migrations.Migration):
    dependencies = [
        ("vault", "0001_initial"),
    ]

    operations = [
        migrations.RunPython(create_fts, drop_fts),
    ]
