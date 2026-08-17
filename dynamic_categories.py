"""Workspace-local dynamic category and article classification metadata."""

from __future__ import annotations

import json
import re
import unicodedata
import uuid
from datetime import datetime, timezone
from pathlib import Path

from markdown_metadata import meta_value as canonical_meta_value
from markdown_metadata import replace_meta as replace_canonical_meta


REGISTRY_REL = "wiki/.categories.json"
RESERVED_NAMES = {"_inbox", "index.md", "log.md", ".", ".."}
CLASSIFICATION_VALUES = {"pending", "confirmed", "sync_conflict"}


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def normalized_key(value: str) -> str:
    return unicodedata.normalize("NFKC", value).strip().casefold()


def validate_name(value: str) -> str:
    if not isinstance(value, str):
        raise ValueError("category name must be a string")
    name = unicodedata.normalize("NFKC", value).strip()
    if not name or len(name) > 80 or normalized_key(name) in RESERVED_NAMES:
        raise ValueError("invalid category name")
    if any(ch in name for ch in "/\\\0") or any(ord(ch) < 32 for ch in name):
        raise ValueError("invalid category name")
    return name


def validate_tag_name(value: str) -> str:
    if not isinstance(value, str):
        raise ValueError("tag name must be a string")
    name = unicodedata.normalize("NFKC", value).strip()
    if not name or len(name) > 50:
        raise ValueError("invalid tag name")
    if any(ch in name for ch in "/\\\0") or any(ord(ch) < 32 for ch in name):
        raise ValueError("invalid tag name")
    if not any(ch.isalnum() for ch in name):
        raise ValueError("invalid tag name")
    return name


def normalize_tags(values: list[str], *, maximum: int = 20) -> list[str]:
    if not isinstance(values, list) or len(values) > maximum:
        raise ValueError(f"tags must contain at most {maximum} items")
    normalized: list[str] = []
    seen: set[str] = set()
    for value in values:
        tag = validate_tag_name(value)
        key = normalized_key(tag)
        if key in seen:
            continue
        seen.add(key)
        normalized.append(tag)
    return normalized


def directory_name(value: str) -> str:
    name = validate_name(value)
    slug = re.sub(r"\s+", "-", name)
    slug = re.sub(r"[^\w\-\u4e00-\u9fff]", "-", slug, flags=re.UNICODE)
    slug = re.sub(r"-+", "-", slug).strip("-.")
    if not slug or normalized_key(slug) in RESERVED_NAMES:
        raise ValueError("category name cannot form a directory")
    return slug[:80]


def empty_registry() -> dict:
    return {"version": 1, "revision": 0, "categories": []}


def load_registry(root: Path) -> dict:
    path = root / REGISTRY_REL
    if not path.is_file():
        return empty_registry()
    data = json.loads(path.read_text(encoding="utf-8"))
    if data.get("version") != 1 or not isinstance(data.get("categories"), list):
        raise ValueError("unsupported category registry")
    return data


def dump_registry(registry: dict) -> str:
    return json.dumps(registry, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def new_category(name: str, *, description: str = "", directory: str | None = None, sort_order: int = 0) -> dict:
    stamp = now_iso()
    return {
        "category_id": uuid.uuid4().hex,
        "name": validate_name(name),
        "directory_name": directory_name(directory or name),
        "description": str(description).strip()[:500],
        "aliases": [],
        "status": "active",
        "sort_order": int(sort_order),
        "created_at": stamp,
        "updated_at": stamp,
    }


def category_by_id(registry: dict, category_id: str, *, include_archived: bool = True) -> dict:
    for item in registry["categories"]:
        if item["category_id"] == category_id and (include_archived or item["status"] == "active"):
            return item
    raise FileNotFoundError(category_id)


def category_by_directory(registry: dict, directory: str) -> dict | None:
    key = normalized_key(directory)
    return next((item for item in registry["categories"] if normalized_key(item["directory_name"]) == key), None)


def assert_unique(registry: dict, candidate: dict, *, ignore_id: str | None = None) -> None:
    name_key = normalized_key(candidate["name"])
    dir_key = normalized_key(candidate["directory_name"])
    for item in registry["categories"]:
        if item["category_id"] == ignore_id:
            continue
        if normalized_key(item["name"]) == name_key:
            raise ValueError("category name already exists")
        if normalized_key(item["directory_name"]) == dir_key:
            raise ValueError("category directory already exists")


def meta_value(md: str, key: str) -> str | None:
    return canonical_meta_value(md, key)


def article_id(md: str) -> str | None:
    value = meta_value(md, "Article-ID")
    return value if value and re.fullmatch(r"[a-f0-9]{32}", value) else None


def tags(md: str) -> list[str]:
    raw = meta_value(md, "Tags") or ""
    values: list[str] = []
    seen: set[str] = set()
    for part in re.split(r"[;,，；]", raw):
        tag = unicodedata.normalize("NFKC", part).strip()[:50]
        key = normalized_key(tag)
        if tag and key not in seen:
            seen.add(key)
            values.append(tag[:50])
    return values[:20]


def classification(md: str) -> str | None:
    value = meta_value(md, "Classification")
    return value if value in CLASSIFICATION_VALUES else None


def replace_meta(md: str, key: str, value: str) -> str:
    return replace_canonical_meta(md, key, value)


def ensure_article_metadata(md: str, *, category_id: str | None, status: str, article_uuid: str | None = None, article_tags: list[str] | None = None) -> str:
    if status not in CLASSIFICATION_VALUES:
        raise ValueError("invalid classification status")
    result = replace_meta(md, "Article-ID", article_uuid or article_id(md) or uuid.uuid4().hex)
    result = replace_meta(result, "Category-ID", category_id or "")
    result = replace_meta(result, "Tags", "; ".join(article_tags if article_tags is not None else tags(md)))
    result = replace_meta(result, "Classification", status)
    result = replace_meta(result, "Classification-Updated", now_iso())
    return result
