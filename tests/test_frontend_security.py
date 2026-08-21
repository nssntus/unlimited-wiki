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


def test_admin_account_invite_ui_keeps_one_time_token_out_of_persistent_client_state():
    root = Path(__file__).parents[1] / "viewer" / "src"
    app = (root / "App.tsx").read_text(encoding="utf-8")
    shell = (root / "layouts" / "admin-shell.tsx").read_text(encoding="utf-8")
    session = (root / "features" / "session-provider.tsx").read_text(encoding="utf-8")
    accounts = (root / "pages" / "admin-accounts-page.tsx").read_text(encoding="utf-8")
    auth = (root / "pages" / "auth-page.tsx").read_text(encoding="utf-8")

    assert '<Route path="/admin/accounts"' in app
    assert 'to="/admin/accounts"' in shell
    assert '"/admin/accounts"' in session
    assert 'apiPost<IssuedRegistrationInvite>(' in accounts
    assert '"/api/admin/registration-invites"' in accounts
    assert "false," in accounts
    assert '#/register?' in accounts
    assert 'session?.registration_mode === "closed"' in accounts
    assert "账号注册已关闭" in accounts
    assert "disabled={registrationClosed}" in accounts
    assert "registrationClosed || creating" in accounts
    assert "setSearchParams({}, { replace: true })" in auth
    assert "localStorage" not in accounts
    assert "sessionStorage" not in accounts
