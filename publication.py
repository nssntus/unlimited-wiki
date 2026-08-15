"""Stable public projection and change fingerprint for private Wiki articles."""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata


FINGERPRINT_VERSION = "v1"
PRIVATE_METADATA = {
    "article-id",
    "category-id",
    "classification",
    "classification-updated",
    "generation",
    "updated",
    "archived",
}
META_RE = re.compile(r"^>\s*([^:]+):\s*(.*)$")
ARTICLE_ID_RE = re.compile(r"^>\s*Article-ID:\s*([a-f0-9]{32})\s*$", re.M | re.I)


def normalize_text(value: object) -> str:
    return unicodedata.normalize("NFC", str(value or "")).strip()


def article_id_from_markdown(markdown: str) -> str | None:
    match = ARTICLE_ID_RE.search(markdown or "")
    return match.group(1).lower() if match else None


def public_markdown(markdown: str) -> str:
    """Remove private runtime metadata and normalize equivalent Markdown bytes."""
    normalized = unicodedata.normalize("NFC", str(markdown or "")).replace("\r\n", "\n").replace("\r", "\n")
    lines: list[str] = []
    for line in normalized.split("\n"):
        match = META_RE.match(line)
        if match:
            key = match.group(1).strip().casefold()
            if key in PRIVATE_METADATA or (key in {"tags", "evidence", "sources", "raw"} and not match.group(2).strip()):
                continue
        lines.append(line.rstrip())
    compact = re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()
    return compact + "\n" if compact else ""


def snapshot_fingerprint(snapshot: dict) -> str:
    projection = {
        "title": normalize_text(snapshot.get("title")),
        "category": normalize_text(snapshot.get("category")),
        "content_status": normalize_text(snapshot.get("content_status")),
        "markdown": public_markdown(str(snapshot.get("markdown", ""))),
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
