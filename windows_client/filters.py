"""Decides which vault files may leave this PC.

Layer 1 - path rules (never synced):
    .obsidian/**, hidden files and folders (leading "." or the Windows
    hidden/system attribute), symlinks and junctions, .env, *.key, *.pem,
    *.crt, *.pfx, secrets*, password*, non-.md files and user-defined globs.
Layer 2 - content scan: notes that look like they contain credentials are
    skipped even when their name is harmless.

The server re-checks the path rules (vault_api/vault/security.py).
"""
import fnmatch
import os
import re
import stat

NOTE_SUFFIX = ".md"
EXCLUDED_NAME_PATTERNS = (".env", "*.key", "*.pem", "*.crt", "*.pfx", "secrets*", "password*")

FILE_ATTRIBUTE_HIDDEN = 0x2
FILE_ATTRIBUTE_SYSTEM = 0x4
FILE_ATTRIBUTE_REPARSE_POINT = 0x400


def name_exclusion_reason(name):
    """Rules for a single file or folder name."""
    lowered = name.lower()
    if lowered == ".obsidian":
        return "obsidian_config"
    if name.startswith("."):
        return "hidden_name"
    for pattern in EXCLUDED_NAME_PATTERNS:
        if fnmatch.fnmatchcase(lowered, pattern):
            return f"pattern:{pattern}"
    return None


def path_exclusion_reason(rel_path, extra_excludes=()):
    """Rules for a vault-relative path using "/" separators."""
    parts = rel_path.split("/")
    for part in parts:
        if not part or part in (".", ".."):
            return "invalid_path"
        reason = name_exclusion_reason(part)
        if reason:
            return reason
    if not parts[-1].lower().endswith(NOTE_SUFFIX):
        return "not_markdown"
    for pattern in extra_excludes:
        if matches_any(rel_path, (pattern,)):
            return f"user_exclude:{pattern}"
    return None


def fs_exclusion_reason(path):
    """Filesystem-level rules for one existing file or folder (not followed if it is a link)."""
    try:
        st = os.lstat(path)
    except FileNotFoundError:
        return "missing"
    except OSError:
        return "unreadable"
    if stat.S_ISLNK(st.st_mode):
        return "symlink"
    attributes = getattr(st, "st_file_attributes", 0)
    if attributes & FILE_ATTRIBUTE_REPARSE_POINT:
        return "reparse_point"  # junctions, cloud placeholders
    if attributes & (FILE_ATTRIBUTE_HIDDEN | FILE_ATTRIBUTE_SYSTEM):
        return "hidden_attribute"
    return None


# High-confidence credential formats only; generic "password: ..." lines are
# too ambiguous to detect reliably.
SECRET_PATTERNS = {
    "private_key": re.compile(r"-----BEGIN (?:[A-Z0-9]+ )*PRIVATE KEY-----"),
    "aws_access_key": re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"),
    "github_token": re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{36,}|github_pat_[A-Za-z0-9_]{50,})"),
    "anthropic_key": re.compile(r"\bsk-ant-[A-Za-z0-9_-]{32,}"),
    "openai_key": re.compile(r"\bsk-(?:proj-|svcacct-|admin-)?[A-Za-z0-9_-]{32,}"),
    "stripe_secret": re.compile(r"\b(?:sk|rk)_live_[A-Za-z0-9]{24,}"),
    "slack_token": re.compile(r"\bxox[abposr]-[A-Za-z0-9-]{10,}"),
    "google_api_key": re.compile(r"\bAIza[0-9A-Za-z_-]{35}"),
    "notion_token": re.compile(r"\b(?:secret_|ntn_)[A-Za-z0-9]{40,}"),
}
# 16 digits in groups of four (Visa/Master/JCB...) or 15 digits as 4-6-5 (Amex),
# confirmed with an issuer prefix and the Luhn checksum.
_CARD_CANDIDATE = re.compile(r"(?<![\d-])(?:\d{4}([ -]?)\d{4}\1\d{4}\1\d{4}|\d{4}([ -]?)\d{6}\2\d{5})(?![\d-])")
_CARD_PREFIX = re.compile(r"^(?:4|5[1-5]|2[2-7]|3[47]|35|6)")


def _luhn_ok(digits):
    total = 0
    for index, char in enumerate(reversed(digits)):
        value = int(char)
        if index % 2 == 1:
            value *= 2
            if value > 9:
                value -= 9
        total += value
    return total % 10 == 0


def find_secret(content):
    """Return the name of the first credential-like pattern found, or None.

    Only the pattern name is reported, never the matched text.
    """
    for name, pattern in SECRET_PATTERNS.items():
        if pattern.search(content):
            return name
    for match in _CARD_CANDIDATE.finditer(content):
        digits = re.sub(r"\D", "", match.group(0))
        if _CARD_PREFIX.match(digits) and _luhn_ok(digits):
            return "card_number"
    return None


def matches_any(rel_path, patterns):
    lowered = rel_path.lower()
    return any(fnmatch.fnmatchcase(lowered, p.lower()) for p in patterns)
