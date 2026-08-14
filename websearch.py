"""Fetch a small, SSRF-safe source set when local evidence is insufficient."""

from __future__ import annotations

import hashlib
import json
import re
from datetime import date
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import parse_qs, quote, unquote, urlencode, urlparse

from security import RemoteError, SafeFetcher, validate_public_url
from storage import atomic_write

SKIP_HOSTS = {"youtube.com", "www.youtube.com", "x.com", "twitter.com", "facebook.com"}
FETCHER = SafeFetcher(timeout=12, max_redirects=5)


def configure_network(*, allow_fake_ip: bool = False) -> None:
    global FETCHER
    FETCHER = SafeFetcher(timeout=12, max_redirects=5, allow_fake_ip=allow_fake_ip)


class _TextExtractor(HTMLParser):
    SKIP = {"script", "style", "nav", "footer", "noscript", "svg", "iframe"}

    def __init__(self) -> None:
        super().__init__()
        self.depth = 0
        self.parts: list[str] = []
        self.title = ""
        self._in_title = False

    def handle_starttag(self, tag: str, attrs) -> None:
        if tag in self.SKIP:
            self.depth += 1
        if tag == "title":
            self._in_title = True
        if tag in {"p", "br", "li", "h1", "h2", "h3", "tr"}:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in self.SKIP and self.depth:
            self.depth -= 1
        if tag == "title":
            self._in_title = False

    def handle_data(self, data: str) -> None:
        if self._in_title:
            self.title += data
        elif self.depth == 0:
            self.parts.append(data)


def _get(url: str, *, data: bytes | None = None, json_only: bool = False) -> tuple[bytes, str]:
    content_types = ("application/json",) if json_only else ("text/html", "text/plain", "application/json")
    raw, final_url, _headers = FETCHER.fetch(
        url,
        method="POST" if data is not None else "GET",
        body=data,
        headers={"Content-Type": "application/x-www-form-urlencoded"} if data is not None else None,
        max_bytes=1024 * 1024 if json_only else 2 * 1024 * 1024,
        content_types=content_types,
    )
    return raw, final_url


def unwrap_ddg(href: str) -> str:
    if href.startswith("//"):
        href = "https:" + href
    parsed = urlparse(href)
    if parsed.hostname and parsed.hostname.endswith("duckduckgo.com") and parsed.path.startswith("/l/"):
        uddg = parse_qs(parsed.query).get("uddg", [""])[0]
        if uddg:
            return unquote(uddg)
    return href


def ddg_urls(query: str, limit: int = 5) -> list[str]:
    try:
        raw, _ = _get("https://lite.duckduckgo.com/lite/?" + urlencode({"q": query[:80]}))
    except RemoteError:
        raw, _ = _get("https://html.duckduckgo.com/html/", data=urlencode({"q": query[:80]}).encode())
    html = raw.decode("utf-8", "replace")
    hrefs = re.findall(r'class="result__a"[^>]*href="([^"]+)"', html)
    if not hrefs:
        hrefs = [unquote(item) for item in re.findall(r'uddg=([^"&]+)', html)]
    out: list[str] = []
    for href in hrefs:
        url = unwrap_ddg(href.replace("&amp;", "&"))
        try:
            canonical, host, _port, _scheme = validate_public_url(url)
        except RemoteError:
            continue
        if host in SKIP_HOSTS or canonical in out:
            continue
        out.append(canonical)
        if len(out) >= limit:
            break
    return out


def wikipedia_hits(query: str, limit: int = 2) -> list[dict]:
    hits: list[dict] = []
    for lang in ("zh", "en"):
        api = f"https://{lang}.wikipedia.org/w/api.php?" + urlencode(
            {"action": "opensearch", "search": query[:80], "limit": limit, "format": "json"}
        )
        try:
            raw, _ = _get(api, json_only=True)
            data = json.loads(raw.decode("utf-8", "replace"))
        except (RemoteError, json.JSONDecodeError):
            continue
        for title, url in zip(data[1] if len(data) > 1 else [], data[3] if len(data) > 3 else []):
            try:
                raw, _ = _get(
                    f"https://{lang}.wikipedia.org/api/rest_v1/page/summary/{quote(title)}",
                    json_only=True,
                )
                summary = json.loads(raw.decode("utf-8", "replace"))
            except (RemoteError, json.JSONDecodeError):
                continue
            text = (summary.get("extract") or "").strip()
            if text:
                hits.append({"title": title, "url": url, "text": text, "published": summary.get("timestamp")})
            if len(hits) >= limit:
                return hits
        if hits:
            break
    return hits


def html_to_text(html: str) -> tuple[str, str]:
    parser = _TextExtractor()
    parser.feed(html)
    title = re.sub(r"\s+", " ", parser.title).strip() or "Untitled"
    text = re.sub(r"[ \t]+", " ", "".join(parser.parts))
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    return title, text


def fetch_page(url: str, max_chars: int = 6000) -> dict | None:
    raw, final_url = _get(url)
    html = raw.decode("utf-8", "replace")
    if b"<" in raw[:200].lower() or b"html" in raw[:200].lower():
        title, text = html_to_text(html)
    else:
        title, text = final_url, html
    if len(text) < 80:
        return None
    return {"title": title, "url": final_url, "text": text[:max_chars], "published": None}


def search_queries(keyword: str, query: str) -> list[str]:
    out: list[str] = []
    for item in (keyword, query):
        item = (item or "").strip()[:80]
        if item and item not in out:
            out.append(item)
    return out[:3]


def search_sources(query: str, limit: int = 3, keyword: str = "") -> list[dict]:
    found: list[dict] = []
    seen: set[str] = set()
    last_error: RemoteError | None = None
    for attempt in search_queries(keyword, query):
        try:
            hits = wikipedia_hits(attempt, limit=2)
        except RemoteError as exc:
            last_error = exc
            hits = []
        for hit in hits:
            if hit["url"] in seen or (keyword and keyword.casefold() not in (hit["title"] + hit["text"]).casefold()):
                continue
            seen.add(hit["url"])
            found.append(hit)
            if len(found) >= limit:
                return found
    try:
        urls = ddg_urls(keyword or query, limit=limit + 2)
    except RemoteError as exc:
        last_error = exc
        urls = []
    for url in urls:
        if url in seen:
            continue
        try:
            page = fetch_page(url)
        except RemoteError as exc:
            last_error = exc
            continue
        if page:
            seen.add(url)
            found.append(page)
        if len(found) >= limit:
            break
    if not found and last_error:
        raise last_error
    return found[:limit]


def raw_markdown(page: dict) -> str:
    title = re.sub(r"[\r\n\x00-\x1f]+", " ", str(page["title"]))[:160].strip() or "Web source"
    source, _host, _port, _scheme = validate_public_url(str(page["url"]))
    published = page.get("published") or "Unknown"
    return (
        f"# {title}\n\n> Source: {source}\n> Collected: {date.today().isoformat()}\n"
        f"> Published: {published}\n\n{str(page['text']).strip()}\n"
    )


def raw_path(page: dict) -> str:
    body = raw_markdown(page).encode("utf-8")
    slug = re.sub(r"[\\/:*?\"<>|]+", "", str(page["title"]))
    slug = re.sub(r"\s+", "-", slug).strip("-")[:50] or "web-source"
    digest = hashlib.sha256(body).hexdigest()[:10]
    return f"raw/web/{date.today().isoformat()}-{slug}-{digest}.md"


def save_raw(project_root: Path, page: dict) -> str:
    rel = raw_path(page)
    dest = project_root / rel
    if not dest.exists():
        atomic_write(dest, raw_markdown(page).encode("utf-8"))
    return rel
