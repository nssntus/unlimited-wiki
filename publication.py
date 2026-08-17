"""Stable public projection and change fingerprint for private Wiki articles."""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata

from markdown_metadata import META_RE, canonical_metadata_preamble, meta_value


FINGERPRINT_VERSION = "v2"
PRIVATE_METADATA = {
    "article-id",
    "category-id",
    "classification",
    "classification-updated",
    "generation",
    "updated",
    "archived",
}
REVIEW_PRIVATE_METADATA = PRIVATE_METADATA | {"category", "raw", "sources", "tags"}
ARTICLE_ID_RE = re.compile(r"^>\s*Article-ID:\s*([a-f0-9]{32})\s*$", re.I)


def normalize_text(value: object) -> str:
    return unicodedata.normalize("NFC", str(value or "")).strip()


def article_id_from_markdown(markdown: str) -> str | None:
    value = meta_value(markdown, "Article-ID")
    return value.lower() if value and re.fullmatch(r"[a-f0-9]{32}", value, re.I) else None


def _project_markdown(markdown: str, private_metadata: set[str]) -> str:
    value = str(markdown or "")
    header = canonical_metadata_preamble(value)
    if header is None:
        return value
    lines, _title_index, block_start, index = header

    visible: list[str] = []
    for line in lines[block_start:index]:
        match = META_RE.match(line.rstrip("\r\n"))
        if match is None:
            visible.append(line)
            continue
        key = match.group(1).strip().casefold()
        if key in private_metadata or (key in {"tags", "evidence", "sources", "raw"} and not match.group(2).strip()):
            continue
        visible.append(line)
    return "".join(lines[:block_start] + visible + lines[index:])


def public_markdown(markdown: str) -> str:
    """Remove private runtime fields from the canonical leading metadata block only."""
    return _project_markdown(markdown, PRIVATE_METADATA)


def review_markdown(markdown: str) -> str:
    """Project public review text without private taxonomy or source metadata."""
    return _project_markdown(markdown, REVIEW_PRIVATE_METADATA)


def fingerprint_markdown(markdown: str) -> str:
    """Normalize encoding-equivalent bytes without changing Markdown whitespace."""
    return unicodedata.normalize("NFC", public_markdown(markdown)).replace("\r\n", "\n").replace("\r", "\n")


def snapshot_fingerprint(snapshot: dict) -> str:
    projection = {
        "title": normalize_text(snapshot.get("title")),
        "category": normalize_text(snapshot.get("category")),
        "content_status": normalize_text(snapshot.get("content_status")),
        "markdown": fingerprint_markdown(str(snapshot.get("markdown", ""))),
    }
    canonical = json.dumps(projection, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return f"{FINGERPRINT_VERSION}:{hashlib.sha256(canonical.encode('utf-8')).hexdigest()}"


def article_fingerprint(article: dict) -> str:
    return snapshot_fingerprint({
        "title": article.get("title"),
        "category": article.get("category"),
        "content_status": article.get("content_status"),
        "markdown": article.get("markdown", ""),
    })
