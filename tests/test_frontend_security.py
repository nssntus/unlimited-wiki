from pathlib import Path


def test_frontend_has_no_runtime_cdn_or_unsafe_html():
    root = Path(__file__).parents[1] / "viewer"
    source = "\n".join(path.read_text(encoding="utf-8") for path in (root / "src").rglob("*.tsx"))
    package = (root / "package-lock.json").read_text(encoding="utf-8")
    assert "dangerouslySetInnerHTML" not in source
    assert "innerHTML" not in source
    assert "jsdelivr" not in source
    assert "react-markdown" in package and "rehype-sanitize" in package


def test_toc_does_not_replace_hash_router_route():
    root = Path(__file__).parents[1] / "viewer" / "src"
    article = (root / "pages" / "article-page.tsx").read_text(encoding="utf-8")
    markdown = (root / "lib" / "markdown-toc.ts").read_text(encoding="utf-8")
    assert 'href={`#${encodeURIComponent(heading)}`}' not in article
    assert "scrollToHeading(heading.id)" in article
    assert "document.getElementById(id) || document.getElementById(headingBase(id))" in markdown


def test_github_pages_script_uses_safe_static_dom_projection():
    root = Path(__file__).parents[1] / "site"
    source = "\n".join(
        (root / name).read_text(encoding="utf-8")
        for name in ("index.html", "language-init.js", "app.js")
    )
    for unsafe in (
        "innerHTML",
        "outerHTML",
        "insertAdjacentHTML",
        "document.write",
        "eval(",
        "new Function",
        "fetch(",
        "XMLHttpRequest",
        "WebSocket",
    ):
        assert unsafe not in source
    assert "textContent" in source
    assert "localStorage.getItem" in source
    assert "localStorage.setItem" in source
    assert "document.documentElement.lang" in source
