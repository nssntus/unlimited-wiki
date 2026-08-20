from __future__ import annotations

import json
import re
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urlparse


ROOT = Path(__file__).resolve().parents[1]
SITE = ROOT / "site"
GITHUB = "https://github.com/nssntus/unlimited-wiki"
PAGES = "https://nssntus.github.io/unlimited-wiki/"
ACTION_PINS = {
    "actions/checkout": "11d5960a326750d5838078e36cf38b85af677262",
    "actions/configure-pages": "983d7736d9b0ae728b81ab479565c72886d7745b",
    "actions/upload-pages-artifact": "56afc609e74202658d3ffba0e8f6dda462b719fa",
    "actions/deploy-pages": "d6db90164ac5ed86f2b6aed7e0febac5b3c0c03e",
}


class DocumentParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.links: list[tuple[str, str, str]] = []
        self.ids: set[str] = set()
        self.meta: dict[tuple[str, str], str] = {}
        self.title = ""
        self._in_title = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = {key: value or "" for key, value in attrs}
        if values.get("id"):
            self.ids.add(values["id"])
        for name in ("href", "src"):
            if values.get(name):
                self.links.append((tag, name, values[name]))
        if tag == "meta":
            key = values.get("name") or values.get("property") or values.get("http-equiv")
            if key:
                self.meta[("meta", key)] = values.get("content", "")
        if tag == "title":
            self._in_title = True

    def handle_endtag(self, tag: str) -> None:
        if tag == "title":
            self._in_title = False

    def handle_data(self, data: str) -> None:
        if self._in_title:
            self.title += data


def parse_document(name: str = "index.html") -> DocumentParser:
    parser = DocumentParser()
    parser.feed((SITE / name).read_text(encoding="utf-8"))
    return parser


def test_pages_content_matches_product_and_readme_contract() -> None:
    index = (SITE / "index.html").read_text(encoding="utf-8")
    script = (SITE / "app.js").read_text(encoding="utf-8")
    combined = index + script

    for phrase in (
        "Unlimited Wiki",
        "Raw",
        "Markdown",
        "协作治理",
        "AI 预审",
        "人工审核",
        "Wiki 广场",
        "Private Workspace",
        "投稿快照",
        "公开版本",
        "React 19 + TypeScript + Vite",
        "Python + SQLite",
    ):
        assert phrase in combined

    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    commands = (
        "git clone https://github.com/nssntus/unlimited-wiki.git",
        "cd unlimited-wiki",
        "python3 -m venv .venv",
        "source .venv/bin/activate",
        "python3 -m pip install -r requirements.txt",
        "npm --prefix viewer ci",
        "npm --prefix viewer run build",
        "python3 serve.py",
    )
    for command in commands:
        assert command in readme
        assert command in index

    assert combined.count(GITHUB) >= 3
    assert "online demo" not in combined.lower()
    assert "not a hosted SaaS or multi-node cluster" in combined
    assert "single-node deployments" in combined
    assert "shared network filesystem" not in combined.lower()


def test_pages_navigation_assets_and_seo_are_subpath_safe() -> None:
    parser = parse_document()
    index = (SITE / "index.html").read_text(encoding="utf-8")

    assert parser.title.strip() == "Unlimited Wiki"
    assert parser.meta[("meta", "description")]
    assert parser.meta[("meta", "og:title")] == "Unlimited Wiki"
    assert parser.meta[("meta", "og:description")]
    assert parser.meta[("meta", "og:type")] == "website"
    assert parser.meta[("meta", "og:url")] == PAGES
    assert parser.meta[("meta", "og:image")].startswith(PAGES)
    assert parser.meta[("meta", "twitter:card")] == "summary"
    assert f'<link rel="canonical" href="{PAGES}"' in index

    for tag, attribute, value in parser.links:
        parsed = urlparse(value)
        if parsed.scheme:
            assert value.startswith((GITHUB, PAGES))
            continue
        if value.startswith("#"):
            assert value[1:] in parser.ids
            continue
        assert value.startswith("./"), (tag, attribute, value)
        assert "../" not in value


def test_language_switcher_is_complete_and_accessible() -> None:
    index = (SITE / "index.html").read_text(encoding="utf-8")
    init = (SITE / "language-init.js").read_text(encoding="utf-8")
    app = (SITE / "app.js").read_text(encoding="utf-8")

    assert 'data-locale-choice="zh-CN" aria-pressed="true"' in index
    assert 'data-locale-choice="en" aria-pressed="false"' in index
    assert 'aria-controls="primary-nav"' in index
    assert 'aria-expanded="false"' in index
    assert "navigator.language" in init
    assert "unlimited-wiki-locale" in init and "unlimited-wiki-locale" in app
    assert '"zh-CN": {' in app and "en: {" in app
    assert "document.documentElement.lang = locale" in app
    assert 'event.key === "Escape"' in app
    assert 'menuToggle.getAttribute("aria-expanded") === "true"' in app
    assert 'element.setAttribute("aria-label", dictionary[key])' in app
    assert 'element.setAttribute("content", dictionary[key])' in app
    assert "aria-live=\"polite\"" in index


def test_manifest_and_404_are_static_pages_compatible() -> None:
    manifest = json.loads((SITE / "manifest.webmanifest").read_text(encoding="utf-8"))
    assert manifest["start_url"] == "./"
    assert manifest["scope"] == "./"
    assert {icon["src"] for icon in manifest["icons"]} == {
        "./apple-touch-icon.png",
        "./favicon.svg",
    }
    assert all(icon["src"].startswith("./") for icon in manifest["icons"])

    missing = (SITE / "404.html").read_text(encoding="utf-8")
    assert '<html lang="en">' in missing
    assert '<base href="/unlimited-wiki/" />' in missing
    assert 'href="./"' in missing
    assert 'src="./favicon.svg"' in missing


def test_pages_apply_a_strict_meta_csp_before_loading_resources() -> None:
    expected = {
        "index.html": {
            "default-src": "'none'",
            "script-src": "'self'",
            "style-src": "'self'",
            "img-src": "'self'",
            "manifest-src": "'self'",
            "base-uri": "'none'",
            "object-src": "'none'",
            "form-action": "'none'",
        },
        "404.html": {
            "default-src": "'none'",
            "style-src": "'self'",
            "img-src": "'self'",
            "base-uri": "'self'",
            "object-src": "'none'",
            "form-action": "'none'",
        },
    }
    for name in ("index.html", "404.html"):
        source = (SITE / name).read_text(encoding="utf-8")
        parser = parse_document(name)
        policy = parser.meta[("meta", "Content-Security-Policy")]
        directives = {
            part.split(maxsplit=1)[0]: part.split(maxsplit=1)[1]
            for part in (item.strip() for item in policy.split(";"))
            if part and " " in part
        }
        assert directives == expected[name]
        assert parser.meta[("meta", "referrer")] == "no-referrer"
        csp_position = source.index('http-equiv="Content-Security-Policy"')
        first_resource = min(
            position
            for token in ("<base ", '<link rel="icon"', '<link rel="stylesheet"', "<script")
            if (position := source.find(token)) >= 0
        )
        assert csp_position < first_resource

    scripts = (SITE / "app.js").read_text(encoding="utf-8")
    init = (SITE / "language-init.js").read_text(encoding="utf-8")
    for source in (scripts, init):
        assert ".style." not in source
        assert 'setAttribute("style"' not in source
    for name in ("index.html", "404.html"):
        source = (SITE / name).read_text(encoding="utf-8")
        assert " style=" not in source
        assert "<style" not in source


def test_styles_include_focus_responsive_and_reduced_motion_contracts() -> None:
    styles = (SITE / "styles.css").read_text(encoding="utf-8")
    assert ":focus-visible" in styles
    assert "@media (prefers-reduced-motion: reduce)" in styles
    assert "@media (max-width: 520px)" in styles
    assert "overflow-x: auto" in styles
    assert "letter-spacing: 0" in styles
    assert "gradient(" not in styles


def test_pages_workflow_only_deploys_the_static_site() -> None:
    workflow = (ROOT / ".github" / "workflows" / "pages.yml").read_text(encoding="utf-8")
    assert "workflow_dispatch:" in workflow
    assert "branches:\n      - main" in workflow
    assert "contents: read" in workflow
    assert "pages: write" in workflow
    assert "id-token: write" in workflow
    uses = re.findall(r"^\s*uses:\s*([^\s#]+)", workflow, flags=re.MULTILINE)
    assert len(uses) == len(ACTION_PINS)
    assert set(uses) == {f"{owner}@{sha}" for owner, sha in ACTION_PINS.items()}
    assert all(owner.startswith("actions/") for owner in ACTION_PINS)
    assert all(re.fullmatch(r"[0-9a-f]{40}", sha) for sha in ACTION_PINS.values())
    assert "persist-credentials: false" in workflow
    assert "path: ./site" in workflow
    assert "viewer" not in workflow
