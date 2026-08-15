"""Position-aware helpers for the canonical Markdown metadata block."""

from __future__ import annotations

import re


META_RE = re.compile(r"^>\s*([^:]+):\s*(.*)$")
TITLE_RE = re.compile(r"^#\s+\S")
CANONICAL_ANCHORS = {"article-id", "category-id", "classification"}
LEGACY_GENERATION_MODES = {"ai-governed", "local+llm", "local-extractive", "seed-adopted", "web+llm"}


def _legacy_generation_value(value: str) -> bool:
    parts = [part.strip() for part in value.split(";")]
    if not parts or parts[0].casefold() not in LEGACY_GENERATION_MODES:
        return False
    fields: dict[str, str] = {}
    for part in parts[1:]:
        key, separator, field_value = part.partition("=")
        if not separator or key.casefold() not in {"task", "state", "source-preserved"}:
            return False
        fields[key.casefold()] = field_value.strip()
    task = fields.get("task")
    state = fields.get("state")
    source_preserved = fields.get("source-preserved")
    return bool(
        (
            task
            and re.fullmatch(r"[a-f0-9]{32}", task, re.I)
            and state in {"queued", "running", "succeeded", "failed", "complete"}
        )
        or (parts[0].casefold() == "local-extractive" and state == "complete")
        or source_preserved == "true"
    )


def canonical_header(markdown: str) -> tuple[list[str], int, int, int] | None:
    """Return lines, title index, metadata start and metadata end."""
    lines = str(markdown or "").splitlines(keepends=True)
    index = 0
    while index < len(lines):
        probe = lines[index].rstrip("\r\n")
        if index == 0:
            probe = probe.lstrip("\ufeff")
        if probe.strip():
            break
        index += 1
    if index >= len(lines):
        return None
    title_probe = lines[index].rstrip("\r\n")
    if index == 0:
        title_probe = title_probe.lstrip("\ufeff")
    if not TITLE_RE.match(title_probe):
        return None

    title_index = index
    index += 1
    while index < len(lines) and not lines[index].rstrip("\r\n").strip():
        index += 1
    block_start = index
    while index < len(lines) and META_RE.match(lines[index].rstrip("\r\n")):
        index += 1
    return lines, title_index, block_start, index


def canonical_metadata_header(markdown: str) -> tuple[list[str], int, int, int] | None:
    header = canonical_header(markdown)
    if header is None:
        return None
    lines, title_index, block_start, block_end = header
    keys = {
        match.group(1).strip().casefold()
        for line in lines[block_start:block_end]
        if (match := META_RE.match(line.rstrip("\r\n")))
    }
    return header if CANONICAL_ANCHORS.issubset(keys) else None


def canonical_metadata_preamble(markdown: str) -> tuple[list[str], int, int, int] | None:
    """Return the anchored block and a recognized legacy runtime block, if present."""
    header = canonical_metadata_header(markdown)
    if header is None:
        return None
    lines, title_index, block_start, block_end = header
    candidate = block_end
    while candidate < len(lines) and not lines[candidate].rstrip("\r\n").strip():
        candidate += 1
    if candidate >= len(lines):
        return header
    index = candidate
    matches: list[re.Match[str]] = []
    while index < len(lines):
        match = META_RE.match(lines[index].rstrip("\r\n"))
        if match is None:
            break
        matches.append(match)
        index += 1
    if not any(
        match.group(1).strip().casefold() == "generation"
        and _legacy_generation_value(match.group(2).strip())
        for match in matches
    ):
        return header
    return lines, title_index, block_start, index


def meta_value(markdown: str, key: str) -> str | None:
    header = canonical_metadata_header(markdown)
    if header is None:
        return None
    lines, _title_index, block_start, block_end = header
    wanted = key.casefold()
    for line in lines[block_start:block_end]:
        match = META_RE.match(line.rstrip("\r\n"))
        if match and match.group(1).strip().casefold() == wanted:
            return match.group(2).strip()
    return None


def replace_meta(markdown: str, key: str, value: str) -> str:
    """Replace or insert metadata without touching same-named body lines."""
    text = str(markdown or "")
    header = canonical_header(text)
    if header is None:
        return text
    lines, title_index, block_start, block_end = header
    if canonical_metadata_header(text) is None:
        block_start = block_end = title_index + 1
    wanted = key.casefold()
    replacement: list[str] = []
    found = False
    for line in lines[block_start:block_end]:
        match = META_RE.match(line.rstrip("\r\n"))
        if match and match.group(1).strip().casefold() == wanted:
            if found:
                continue
            ending = "\r\n" if line.endswith("\r\n") else "\n" if line.endswith("\n") else ""
            replacement.append(f"> {key}: {value}{ending}")
            found = True
        else:
            replacement.append(line)
    if found:
        return "".join(lines[:block_start] + replacement + lines[block_end:])

    ending = "\r\n" if "\r\n" in text else "\n"
    insert_at = block_start if block_end > block_start else title_index + 1
    if not lines[title_index].endswith(("\n", "\r")):
        lines[title_index] += ending
    lines.insert(insert_at, f"> {key}: {value}{ending}")
    return "".join(lines)
