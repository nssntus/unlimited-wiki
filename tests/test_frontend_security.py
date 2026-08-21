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


def test_worker_unavailable_ui_distinguishes_queued_from_running():
    root = Path(__file__).parents[1] / "viewer" / "src"
    api = (root / "lib" / "api.ts").read_text(encoding="utf-8")
    article = (root / "pages" / "article-page.tsx").read_text(encoding="utf-8")
    tasks = (root / "pages" / "tasks-page.tsx").read_text(encoding="utf-8")
    submissions = (root / "pages" / "submissions-page.tsx").read_text(encoding="utf-8")
    generate = (root / "features" / "generation-dialog.tsx").read_text(encoding="utf-8")
    share = (root / "pages" / "share-page.tsx").read_text(encoding="utf-8")
    health = (root / "pages" / "health-page.tsx").read_text(encoding="utf-8")
    ingest = (root / "pages" / "ingest-page.tsx").read_text(encoding="utf-8")

    assert 'task.status === "queued"' in article and '"排队中"' in article
    assert 'task.status === "running"' in article and '"生成中"' in article
    assert "taskAvailable(system, task)" in article
    assert 'item.status === "queued" && isTaskAvailable(system, item)' in tasks
    assert "Promise.all([tasks.refetch(), status.refetch()])" in tasks
    assert 'status.isError ? <Alert' in tasks and "status.refetch()" in tasks
    assert "后台生成服务未启用" in tasks
    assert 'status === "ai_queued" && Boolean(system?.platform_review.enabled)' in submissions
    assert "AI 预审服务未启用" in submissions
    assert "data.remote_task.required && !data.remote_task.available" in generate
    assert "!system?.platform_review.enabled" in share
    for source in (article, submissions, share, health, ingest):
        assert "status.isError" in source
        assert "status.refetch()" in source
    assert "capabilityUnknown" in ingest
    assert "return query.isError ? undefined : query.data" in api


def test_refetch_error_discards_previously_successful_worker_capabilities():
    root = Path(__file__).parents[1] / "viewer" / "src"
    api = (root / "lib" / "api.ts").read_text(encoding="utf-8")
    capability_pages = [
        root / "pages" / name
        for name in (
            "article-page.tsx",
            "tasks-page.tsx",
            "share-page.tsx",
            "submissions-page.tsx",
            "health-page.tsx",
            "ingest-page.tsx",
        )
    ]

    assert "export function currentSystemStatus" in api
    assert "return query.isError ? undefined : query.data" in api
    for path in capability_pages:
        source = path.read_text(encoding="utf-8")
        assert "currentSystemStatus(status)" in source
        assert "status.data" not in source

    share = capability_pages[2].read_text(encoding="utf-8")
    submissions = capability_pages[3].read_text(encoding="utf-8")
    health = capability_pages[4].read_text(encoding="utf-8")
    ingest = capability_pages[5].read_text(encoding="utf-8")
    assert "!system?.platform_review.enabled" in share
    assert "disabled={!system?.platform_review.enabled}" in submissions
    assert "const polling = shouldPollReview(data.status, system)" in submissions
    assert ": polling && <Alert" in submissions
    assert ": active.has(data.status) && <Alert" not in submissions
    assert "!governanceAvailable" in health
    assert "capabilityUnknown" in ingest
