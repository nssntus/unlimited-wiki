"""Fetch a few web sources when the local wiki cannot explain a keyword."""

from __future__ import annotations

import json
import re
import ssl
from datetime import date
from html.parser import HTMLParser
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, quote, unquote, urlencode, urlparse
from urllib.request import HTTPSHandler, Request, build_opener, urlopen

UA = "Mozilla/5.0 (compatible; wiki-keyword-agent/1.0)"
SKIP_HOSTS = {"youtube.com", "www.youtube.com", "x.com", "twitter.com", "facebook.com"}


class _TextExtractor(HTMLParser):
    SKIP = {"script", "style", "nav", "footer", "noscript", "svg"}

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
            return
        if self.depth == 0:
            self.parts.append(data)


def _get(url: str, timeout: int = 12, data: bytes | None = None) -> bytes:
    req = Request(url, data=data, headers={"User-Agent": UA})
    try:
        with urlopen(req, timeout=timeout) as resp:
            return resp.read()
    except URLError as exc:
        if "CERTIFICATE_VERIFY_FAILED" not in str(exc):
            raise
        opener = build_opener(HTTPSHandler(context=ssl._create_unverified_context()))
        with opener.open(req, timeout=timeout) as resp:
            return resp.read()


def unwrap_ddg(href: str) -> str:
    if href.startswith("//"):
        href = "https:" + href
    parsed = urlparse(href)
    if "duckduckgo.com" in parsed.netloc and parsed.path.startswith("/l/"):
        uddg = parse_qs(parsed.query).get("uddg", [""])[0]
        if uddg:
            return unquote(uddg)
    return href


def ddg_urls(query: str, limit: int = 5) -> list[str]:
    raw = ""
    try:
        raw = _get("https://lite.duckduckgo.com/lite/?" + urlencode({"q": query})).decode(
            "utf-8", "replace"
        )
    except (URLError, HTTPError, TimeoutError):
        body = urlencode({"q": query}).encode()
        raw = _get("https://html.duckduckgo.com/html/", data=body).decode("utf-8", "replace")
    hrefs = re.findall(r'class="result__a"[^>]*href="([^"]+)"', raw)
    if not hrefs:
        hrefs = re.findall(r'uddg=([^"&]+)', raw)
        hrefs = [unquote(item) for item in hrefs]
    out: list[str] = []
    seen: set[str] = set()
    for href in hrefs:
        url = unwrap_ddg(href.replace("&amp;", "&"))
        host = urlparse(url).netloc.lower().removeprefix("www.")
        if not url.startswith("http") or host in SKIP_HOSTS or url in seen:
            continue
        seen.add(url)
        out.append(url)
        if len(out) >= limit:
            break
    return out


def wikipedia_hits(query: str, limit: int = 2) -> list[dict]:
    hits = []
    for lang in ("zh", "en"):
        api = (
            f"https://{lang}.wikipedia.org/w/api.php?"
            + urlencode({"action": "opensearch", "search": query, "limit": limit, "format": "json"})
        )
        try:
            data = json.loads(_get(api).decode("utf-8", "replace"))
        except (URLError, HTTPError, TimeoutError, json.JSONDecodeError):
            continue
        titles = data[1] if len(data) > 1 else []
        urls = data[3] if len(data) > 3 else []
        for title, url in zip(titles, urls):
            summary_url = f"https://{lang}.wikipedia.org/api/rest_v1/page/summary/{quote(title)}"
            try:
                summary = json.loads(_get(summary_url).decode("utf-8", "replace"))
            except (URLError, TimeoutError, json.JSONDecodeError):
                continue
            extract = (summary.get("extract") or "").strip()
            if not extract:
                continue
            hits.append({"title": title, "url": url, "text": extract})
            if len(hits) >= limit:
                return hits
        if hits:
            return hits
    return hits


def html_to_text(html: str) -> tuple[str, str]:
    parser = _TextExtractor()
    try:
        parser.feed(html)
    except Exception:
        pass
    title = re.sub(r"\s+", " ", parser.title).strip() or "Untitled"
    text = re.sub(r"[ \t]+", " ", "".join(parser.parts))
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    return title, text


def fetch_page(url: str, max_chars: int = 6000) -> dict | None:
    try:
        raw = _get(url, timeout=12)
    except (URLError, TimeoutError, ValueError):
        return None
    if b"<" not in raw[:200].lower() and b"html" not in raw[:200].lower():
        try:
            text = raw.decode("utf-8", "replace")
        except Exception:
            return None
        return {"title": url, "url": url, "text": text[:max_chars]}
    html = raw.decode("utf-8", "replace")
    title, text = html_to_text(html)
    if len(text) < 80:
        return None
    return {"title": title, "url": url, "text": text[:max_chars]}


def search_queries(keyword: str, query: str) -> list[str]:
    out: list[str] = []
    for item in (keyword, query):
        item = (item or "").strip()
        if item and item not in out:
            out.append(item)
    for en in re.findall(r"[A-Za-z][A-Za-z0-9]+(?:[ \-][A-Za-z][A-Za-z0-9]+)+", query or ""):
        if en not in out:
            out.append(en)
    return out[:3]


def search_sources(query: str, limit: int = 3, keyword: str = "") -> list[dict]:
    found: list[dict] = []
    seen: set[str] = set()
    for attempt in search_queries(keyword, query):
        try:
            for hit in wikipedia_hits(attempt, limit=2):
                if hit["url"] in seen or len(hit["text"]) < 40:
                    continue
                # skip obvious off-sense wiki stubs
                if keyword and keyword.lower() not in (hit["title"] + hit["text"]).lower():
                    continue
                seen.add(hit["url"])
                found.append(hit)
                if len(found) >= limit:
                    return found
        except (URLError, HTTPError, TimeoutError, json.JSONDecodeError, ValueError):
            continue
    try:
        urls = ddg_urls(keyword or query, limit=limit + 2)
    except (URLError, HTTPError, TimeoutError, ValueError):
        urls = []
    for url in urls:
        if url in seen:
            continue
        page = fetch_page(url)
        if not page:
            continue
        seen.add(url)
        found.append(page)
        if len(found) >= limit:
            break
    return found[:limit]


def save_raw(project_root: Path, page: dict) -> str:
    folder = project_root / "raw" / "web"
    folder.mkdir(parents=True, exist_ok=True)
    slug = re.sub(r"[\\/:*?\"<>|]+", "", page["title"])
    slug = re.sub(r"\s+", "-", slug).strip("-")[:50] or "web-source"
    name = f"{date.today().isoformat()}-{slug}.md"
    dest = folder / name
    n = 2
    while dest.exists():
        dest = folder / f"{date.today().isoformat()}-{slug}-{n}.md"
        n += 1
    body = (
        f"# {page['title']}\n\n"
        f"> Source: {page['url']}\n"
        f"> Collected: {date.today().isoformat()}\n"
        f"> Published: Unknown\n\n"
        f"{page['text'].strip()}\n"
    )
    dest.write_text(body, encoding="utf-8")
    return dest.relative_to(project_root).as_posix()
