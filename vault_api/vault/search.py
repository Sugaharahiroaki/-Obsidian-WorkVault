"""Full-text search over mirrored notes.

On SQLite the ``vault_note_fts`` FTS5 table (see migration 0002) uses the
trigram tokenizer: it matches any substring of three or more characters, so
Japanese text works without a word segmenter. Terms shorter than three
characters (common in Japanese, e.g. "税" or "契約"), queries FTS cannot
answer, and non-SQLite databases fall back to case-insensitive LIKE.
"""
import re
from dataclasses import dataclass

from django.db import DatabaseError, connection
from django.db.models import Case, IntegerField, Q, Value, When

from .models import VaultNote

FTS_TABLE = "vault_note_fts"
TRIGRAM_MIN_CHARS = 3
MAX_TERMS = 10
# bm25 column weights: title, path, content.
BM25_WEIGHTS = (10.0, 5.0, 1.0)


@dataclass
class SearchResult:
    mode: str  # "fts", "like" or "none"
    terms: list
    notes: list


def split_terms(query):
    """Whitespace-separated terms (ASCII or full-width space), de-duplicated."""
    terms = []
    for term in query.replace("　", " ").split():
        if term not in terms:
            terms.append(term)
    return terms[:MAX_TERMS]


def fts_available():
    if connection.vendor != "sqlite":
        return False
    with connection.cursor() as cursor:
        cursor.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = %s", [FTS_TABLE])
        return cursor.fetchone() is not None


def search(query, *, limit, folder=""):
    terms = split_terms(query)
    if not terms:
        return SearchResult(mode="none", terms=[], notes=[])

    if all(len(term) >= TRIGRAM_MIN_CHARS for term in terms) and fts_available():
        try:
            notes = _fts_search(terms, limit=limit, folder=folder)
        except DatabaseError:
            notes = []
        if notes:
            return SearchResult(mode="fts", terms=terms, notes=notes)

    return SearchResult(mode="like", terms=terms, notes=_like_search(terms, limit=limit, folder=folder))


def _fts_search(terms, *, limit, folder):
    # Each term becomes a quoted phrase, so user input cannot inject FTS5
    # query syntax (AND/OR/NEAR/column filters). Terms are ANDed.
    match = " AND ".join('"' + term.replace('"', '""') + '"' for term in terms)
    sql = [
        f"SELECT n.id FROM {FTS_TABLE} JOIN vault_vaultnote AS n ON n.id = {FTS_TABLE}.rowid",
        f"WHERE {FTS_TABLE} MATCH %s",
    ]
    params = [match]
    if folder:
        sql.append("AND (n.folder = %s OR n.folder LIKE %s ESCAPE '\\')")
        params += [folder, _like_escape(folder) + "/%"]
    sql.append(f"ORDER BY bm25({FTS_TABLE}, %s, %s, %s) LIMIT %s")
    params += [*BM25_WEIGHTS, limit]

    with connection.cursor() as cursor:
        cursor.execute(" ".join(sql), params)
        ids = [row[0] for row in cursor.fetchall()]
    by_id = VaultNote.objects.in_bulk(ids)
    return [by_id[i] for i in ids if i in by_id]


def _like_search(terms, *, limit, folder):
    qs = VaultNote.objects.all()
    for term in terms:
        qs = qs.filter(Q(title__icontains=term) | Q(path__icontains=term) | Q(content__icontains=term))
    if folder:
        qs = qs.filter(Q(folder=folder) | Q(folder__startswith=folder + "/"))
    title_hit = Case(
        When(title__icontains=terms[0], then=Value(1)),
        default=Value(0),
        output_field=IntegerField(),
    )
    return list(qs.annotate(title_hit=title_hit).order_by("-title_hit", "-updated_at")[:limit])


def _like_escape(value):
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def make_snippet(content, terms, radius=60):
    """A short single-line excerpt around the first matching term."""
    pattern = re.compile(
        "|".join(re.escape(t) for t in sorted(terms, key=len, reverse=True)), re.IGNORECASE
    )
    match = pattern.search(content) if terms else None
    if match is None:
        body = _strip_frontmatter(content)
        excerpt = body[: radius * 2]
        suffix = "…" if len(body) > len(excerpt) else ""
        return _one_line(excerpt) + suffix
    start = max(0, match.start() - radius)
    end = min(len(content), match.end() + radius)
    prefix = "…" if start > 0 else ""
    suffix = "…" if end < len(content) else ""
    return prefix + _one_line(content[start:end]) + suffix


def _strip_frontmatter(content):
    if content.startswith("---"):
        end = content.find("\n---", 3)
        if end != -1:
            return content[end + 4 :].lstrip("\r\n")
    return content


def _one_line(text):
    return " ".join(text.split())
