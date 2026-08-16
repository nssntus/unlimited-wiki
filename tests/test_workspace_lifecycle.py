from __future__ import annotations

import contextlib
import threading
from pathlib import Path

import pytest

from serve import create_app, create_server
from publication import public_markdown, snapshot_fingerprint
from state_store import StateStore
from tests.test_multitenant import Client, context_for
from tests.test_team_collaboration import _create_team, _invite_and_accept
from wiki_service import LLMConfig


@pytest.fixture
def lifecycle_server(tmp_path: Path):
    (tmp_path / "viewer" / "dist").mkdir(parents=True)
    (tmp_path / "viewer" / "dist" / "index.html").write_text(
        "<!doctype html><main id='root'></main>", encoding="utf-8",
    )
    app = create_app(tmp_path, tmp_path / "viewer", start_worker=False, multi_user=True)
    server = create_server(app, host="127.0.0.1", port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://{server.server_address[0]}:{server.server_address[1]}"
    try:
        yield app, base
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
        app.close()


def _team_world(lifecycle_server):
    app, base = lifecycle_server
    owner, member = Client(base), Client(base)
    owner.register("lifecycle-owner@example.com", "Lifecycle Owner")
    member.register("lifecycle-member@example.com", "Lifecycle Member")
    owner_personal = context_for(app, owner).workspace_id
    member_personal = context_for(app, member).workspace_id
    team = _create_team(owner, "Lifecycle Team", key="lifecycle-create")
    owner.request("POST", "/api/workspaces/switch", {"workspace_id": team["id"]}, key="owner-team")
    _invite_and_accept(owner, member, "lifecycle-member@example.com", "editor")
    member.request("POST", "/api/workspaces/switch", {"workspace_id": team["id"]}, key="member-team")
    return app, owner, member, team, owner_personal, member_personal


def test_suspend_keeps_identity_session_and_restore_requires_explicit_switch(lifecycle_server):
    app, owner, member, team, owner_personal, member_personal = _team_world(lifecycle_server)
    service = app.workspace_service(context_for(app, owner))
    task, _ = service.state.enqueue_task(
        "supplement", "paused subject", {"path": "concepts/missing.md"}, actor_user_id=context_for(app, owner).user_id,
    )

    suspended = owner.request("POST", f"/api/workspaces/{team['id']}/suspend", {}, key="suspend-team")
    assert suspended[0] == 200 and suspended[1]["status"] == "suspended"
    assert team["id"] not in app._services
    assert service.state.get_task(task["id"])["status"] == "paused"
    assert StateStore(service.root).get_task(task["id"])["status"] == "paused"

    for client in (owner, member):
        status, session = client.request("GET", "/api/auth/session")
        assert status == 200
        assert session["authenticated"] is True
        assert session["workspace"] is None
        assert session["workspace_selection_required"] is True
        status, error = client.request("GET", "/api/articles")
        assert status == 409 and error["code"] == "workspace_selection_required"

    owner_spaces = owner.request("GET", "/api/workspaces?include_inactive=1")[1]
    suspended_team = next(item for item in owner_spaces if item["id"] == team["id"])
    assert suspended_team["can_restore"] is True and suspended_team["can_delete"] is True
    member_spaces = member.request("GET", "/api/workspaces?include_inactive=1")[1]
    assert next(item for item in member_spaces if item["id"] == team["id"])["can_leave"] is True

    restored = owner.request("POST", f"/api/workspaces/{team['id']}/restore", {}, key="restore-team")
    assert restored[0] == 200 and restored[1]["status"] == "active"
    assert StateStore(app.platform.workspace_root(team["id"]), recover_running=False).get_task(task["id"])["status"] == "queued"
    assert owner.request("GET", "/api/auth/session")[1]["workspace_selection_required"] is True
    assert owner.request(
        "POST", "/api/workspaces/switch", {"workspace_id": team["id"]}, key="switch-restored",
    )[1]["workspace"]["id"] == team["id"]
    assert member.request(
        "POST", "/api/workspaces/switch", {"workspace_id": member_personal}, key="member-personal",
    )[1]["workspace"]["id"] == member_personal
    assert owner_personal != member_personal


def test_soft_delete_preserves_workspace_data_and_terminates_tasks(lifecycle_server):
    app, owner, _member, team, _owner_personal, _member_personal = _team_world(lifecycle_server)
    context = context_for(app, owner)
    service = app.workspace_service(context)
    root = service.root
    article = root / "wiki" / "concepts" / "retained.md"
    article.parent.mkdir(parents=True, exist_ok=True)
    article.write_text("# Retained\n", encoding="utf-8")
    app.platform.save_model(team["id"], "openai-compatible", "https://example.com/v1", "secret", "model")
    task, _ = service.state.enqueue_task(
        "supplement", "deleted subject", {"path": "concepts/retained.md"}, actor_user_id=context.user_id,
    )

    assert owner.request("POST", f"/api/workspaces/{team['id']}/suspend", {}, key="delete-suspend")[0] == 200
    deleted = owner.request("POST", f"/api/workspaces/{team['id']}/delete", {}, key="delete-team")
    assert deleted[0] == 200 and deleted[1]["status"] == "deleted"
    assert root.is_dir() and article.read_text(encoding="utf-8") == "# Retained\n"
    assert app.platform.load_model(team["id"])["model"] == "model"
    assert StateStore(root, recover_running=False).get_task(task["id"])["error_type"] == "workspace_deleted"
    assert owner.request("POST", f"/api/workspaces/{team['id']}/restore", {}, key="restore-deleted")[0] == 422
    assert owner.request(
        "POST", "/api/workspaces/switch", {"workspace_id": team["id"]}, key="switch-deleted",
    )[0] == 404


def test_member_can_leave_team_but_owner_must_transfer_first(lifecycle_server):
    app, owner, member, team, _owner_personal, _member_personal = _team_world(lifecycle_server)
    member_id = context_for(app, member).user_id
    assert owner.request("POST", f"/api/workspaces/{team['id']}/leave", {}, key="owner-leave")[0] == 422
    left = member.request("POST", f"/api/workspaces/{team['id']}/leave", {}, key="member-leave")
    assert left == (200, {"workspace_id": team["id"], "status": "left"})
    session = member.request("GET", "/api/auth/session")[1]
    assert session["authenticated"] is True and session["workspace_selection_required"] is True
    with app.platform.connect() as db:
        row = db.execute(
            "SELECT status,is_default FROM workspace_members WHERE workspace_id=? AND user_id=?",
            (team["id"], member_id),
        ).fetchone()
        assert tuple(row) == ("suspended", 0)
    assert owner.request("GET", "/api/notifications")[0] == 200


def test_lifecycle_rejects_personal_low_role_and_foreign_workspace(lifecycle_server):
    app, owner, member, team, owner_personal, _member_personal = _team_world(lifecycle_server)
    assert owner.request("POST", f"/api/workspaces/{owner_personal}/suspend", {}, key="personal-suspend")[0] == 422
    assert member.request("POST", f"/api/workspaces/{team['id']}/suspend", {}, key="editor-suspend")[0] == 403
    outsider = Client(member.origin)
    outsider.register("lifecycle-outsider@example.com", "Lifecycle Outsider")
    assert outsider.request("POST", f"/api/workspaces/{team['id']}/suspend", {}, key="foreign-suspend")[0] == 404


def test_stale_service_cannot_write_after_suspend(lifecycle_server):
    app, owner, _member, team, _owner_personal, _member_personal = _team_world(lifecycle_server)
    context = context_for(app, owner)
    service = app.workspace_service(context)
    article_path = service.root / "wiki" / "concepts" / "guarded.md"
    article_path.parent.mkdir(parents=True, exist_ok=True)
    article_path.write_text("# Guarded\n\nBefore.\n", encoding="utf-8")
    article = service.read_article("concepts/guarded.md")
    captured = threading.Event()
    release = threading.Event()
    outcome: dict = {}

    def stale_request():
        captured.set()
        assert release.wait(3)
        try:
            with app.workspace_action(context, service, "wiki.write"):
                outcome["result"] = service.save_article(
                    article["path"], article["markdown"] + "\nAfter.\n", article["revision"],
                )
        except Exception as exc:
            outcome["error"] = exc

    thread = threading.Thread(target=stale_request)
    thread.start()
    assert captured.wait(3)
    before = article_path.read_bytes()
    assert owner.request("POST", f"/api/workspaces/{team['id']}/suspend", {}, key="guard-suspend")[0] == 200
    release.set()
    thread.join(timeout=3)
    assert not thread.is_alive()
    assert isinstance(outcome.get("error"), FileNotFoundError)
    assert article_path.read_bytes() == before
    assert team["id"] not in app._services


@pytest.mark.parametrize("request_kind", ["article", "ingest"])
def test_private_get_cannot_run_or_recache_after_suspend(lifecycle_server, monkeypatch, request_kind):
    app, owner, _member, team, _owner_personal, _member_personal = _team_world(lifecycle_server)
    context = context_for(app, owner)
    service = app.workspace_service(context)
    article_path = service.root / "wiki" / "concepts" / "guarded-get.md"
    article_path.parent.mkdir(parents=True, exist_ok=True)
    article_path.write_text("# Guarded GET\n\nPrivate body.\n", encoding="utf-8")
    raw_path = service.root / "raw" / "local" / "guarded-get.md"
    raw_path.parent.mkdir(parents=True, exist_ok=True)
    raw_path.write_text("# Guarded Raw\n\nPrivate raw body.\n", encoding="utf-8")
    service.llm = LLMConfig(base_url="https://example.com/v1", api_key="test-key", model="test-model")
    before_task_ids = {item["id"] for item in service.state.list_tasks()}
    calls = 0
    method_name = "read_article" if request_kind == "article" else "ingest_preview"
    original_method = getattr(service, method_name)

    def observed(*args, **kwargs):
        nonlocal calls
        calls += 1
        return original_method(*args, **kwargs)

    monkeypatch.setattr(service, method_name, observed)
    original_action = app.workspace_action
    captured = threading.Event()
    release = threading.Event()
    blocked = False

    @contextlib.contextmanager
    def delayed_action(action_context, action_service, permission):
        nonlocal blocked
        if not blocked:
            blocked = True
            captured.set()
            assert release.wait(3)
        with original_action(action_context, action_service, permission):
            yield

    monkeypatch.setattr(app, "workspace_action", delayed_action)
    path = (
        "/api/article?path=concepts/guarded-get.md"
        if request_kind == "article"
        else "/api/ingest/preview?path=raw/local/guarded-get.md"
    )
    outcome: dict = {}
    request = threading.Thread(target=lambda: outcome.setdefault("response", owner.request("GET", path)))
    request.start()
    assert captured.wait(3)
    assert owner.request("POST", f"/api/workspaces/{team['id']}/suspend", {}, key=f"get-{request_kind}-suspend")[0] == 200
    release.set()
    request.join(timeout=3)

    assert not request.is_alive()
    assert outcome["response"][0] == 409
    assert calls == 0
    assert {item["id"] for item in service.state.list_tasks()} == before_task_ids
    assert team["id"] not in app._services
    assert team["id"] not in app._diagnostics


def test_ingest_preview_task_records_current_actor(lifecycle_server):
    app, owner, _member, _team, _owner_personal, _member_personal = _team_world(lifecycle_server)
    context = context_for(app, owner)
    service = app.workspace_service(context)
    raw_path = service.root / "raw" / "local" / "actor-preview.md"
    raw_path.parent.mkdir(parents=True, exist_ok=True)
    raw_path.write_text("# Actor Preview\n\nRaw body.\n", encoding="utf-8")
    service.llm = LLMConfig(base_url="https://example.com/v1", api_key="test-key", model="test-model")

    response = owner.request("GET", "/api/ingest/preview?path=raw/local/actor-preview.md")

    assert response[0] == 200
    task_id = response[1]["classification_plan"]["task_id"]
    assert service.state.get_task(task_id)["actor_user_id"] == context.user_id


def test_lifecycle_replay_reconciles_authoritative_status(lifecycle_server):
    app, owner, _member, team, _owner_personal, _member_personal = _team_world(lifecycle_server)
    service = app.workspace_service(context_for(app, owner))
    task, _ = service.state.enqueue_task("supplement", "replay", {"path": "concepts/missing.md"})

    assert owner.request("POST", f"/api/workspaces/{team['id']}/suspend", {}, key="replay-suspend")[1]["status"] == "suspended"
    assert owner.request("POST", f"/api/workspaces/{team['id']}/restore", {}, key="replay-restore")[1]["status"] == "active"
    stale_suspend = owner.request("POST", f"/api/workspaces/{team['id']}/suspend", {}, key="replay-suspend")
    assert stale_suspend[0] == 200
    assert stale_suspend[1]["status"] == "active"
    assert stale_suspend[1]["can_suspend"] is True
    assert stale_suspend[1]["can_restore"] is False
    assert stale_suspend[1]["can_delete"] is False

    record = app.platform.workspace_storage_state(team["id"])
    current = StateStore(app.platform.workspace_root(record["root_name"]), recover_running=False).get_task(task["id"])
    assert record["status"] == "active"
    assert current["status"] == "queued"

    assert owner.request("POST", f"/api/workspaces/{team['id']}/suspend", {}, key="replay-suspend-new")[1]["status"] == "suspended"
    stale_restore = owner.request("POST", f"/api/workspaces/{team['id']}/restore", {}, key="replay-restore")
    assert stale_restore[0] == 200
    assert stale_restore[1]["status"] == "suspended"
    assert stale_restore[1]["can_suspend"] is False
    assert stale_restore[1]["can_restore"] is True
    assert stale_restore[1]["can_delete"] is True
    assert StateStore(service.root, recover_running=False).get_task(task["id"])["status"] == "paused"


def test_lifecycle_replay_repairs_failed_local_projection(lifecycle_server, monkeypatch):
    app, owner, _member, team, _owner_personal, _member_personal = _team_world(lifecycle_server)
    service = app.workspace_service(context_for(app, owner))
    task, _ = service.state.enqueue_task("supplement", "repair", {"path": "concepts/missing.md"})
    original = app._reconcile_workspace_storage
    calls = 0

    def fail_once(record, cached_service=None):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise OSError("injected lifecycle projection failure")
        return original(record, cached_service)

    monkeypatch.setattr(app, "_reconcile_workspace_storage", fail_once)
    first = owner.request("POST", f"/api/workspaces/{team['id']}/suspend", {}, key="repair-suspend")
    assert first[0] == 500
    assert app.platform.workspace_storage_state(team["id"])["status"] == "suspended"

    replay = owner.request("POST", f"/api/workspaces/{team['id']}/suspend", {}, key="repair-suspend")
    assert replay[0] == 200
    state = StateStore(service.root, recover_running=False)
    assert state.get_task(task["id"])["status"] == "paused"


def test_suspend_pauses_running_task_and_fences_late_result(lifecycle_server, monkeypatch):
    app, owner, _member, team, _owner_personal, _member_personal = _team_world(lifecycle_server)
    context = context_for(app, owner)
    service = app.workspace_service(context)
    article_path = service.root / "wiki" / "concepts" / "late.md"
    article_path.parent.mkdir(parents=True, exist_ok=True)
    article_path.write_text("# Late result\n\nBefore.\n", encoding="utf-8")
    article = service.read_article("concepts/late.md")
    task, _ = service.state.enqueue_task(
        "governance", article["title"], {"path": article["path"], "base_revision": article["revision"]},
        actor_user_id=context.user_id,
    )
    claimed = service.state.claim_task({"governance"})
    model_started = threading.Event()
    release_model = threading.Event()

    def delayed_model(_article):
        model_started.set()
        assert release_model.wait(3)
        return article["markdown"] + "\nLate mutation.\n"

    monkeypatch.setattr(service, "_call_governance_llm", delayed_model)
    monkeypatch.setattr("wiki_ops.article_quality_issues", lambda *_args, **_kwargs: [])
    outcome: dict = {}

    def run_task():
        try:
            outcome["result"] = service._run_governance_task(claimed)
        except Exception as exc:
            outcome["error"] = exc

    thread = threading.Thread(target=run_task)
    thread.start()
    assert model_started.wait(3)
    before = article_path.read_bytes()
    assert owner.request("POST", f"/api/workspaces/{team['id']}/suspend", {}, key="late-suspend")[0] == 200
    assert service.state.get_task(task["id"])["status"] == "paused"
    release_model.set()
    thread.join(timeout=3)
    assert not thread.is_alive()
    assert outcome.get("error") is None
    assert outcome["result"]["superseded"] is True
    assert article_path.read_bytes() == before
    assert list(service.files.history_root.glob(f"govern-{task['id']}-attempt-*")) == []

    assert owner.request("POST", f"/api/workspaces/{team['id']}/restore", {}, key="late-restore")[0] == 200
    assert StateStore(service.root, recover_running=False).get_task(task["id"])["status"] == "queued"


@pytest.mark.parametrize(
    ("error_type", "retry"),
    [("temporary_network", True), ("model_error", False)],
)
def test_suspend_serializes_worker_failure_finalization(lifecycle_server, monkeypatch, error_type, retry):
    app, owner, _member, team, _owner_personal, _member_personal = _team_world(lifecycle_server)
    context = context_for(app, owner)
    service = app.workspace_service(context)
    payload = {
        "raw_path": "raw/local/failure.md",
        "raw_revision": "raw-revision",
        "taxonomy_revision": 1,
    }
    task, _ = service.state.enqueue_task(
        "raw-classification-plan", "failure-plan", payload, actor_user_id=context.user_id,
    )
    claimed = service.state.claim_task({"raw-classification-plan"})
    service.state.save_raw_classification_plan(
        payload["raw_path"], payload["raw_revision"], payload["taxonomy_revision"],
        "running", task_id=task["id"],
    )
    platform_committed = threading.Event()
    release_reconcile = threading.Event()
    original_reconcile = app._reconcile_workspace_storage

    def delayed_reconcile(record, cached_service=None):
        if record["id"] == team["id"] and record["status"] == "suspended":
            platform_committed.set()
            assert release_reconcile.wait(3)
        return original_reconcile(record, cached_service)

    monkeypatch.setattr(app, "_reconcile_workspace_storage", delayed_reconcile)
    suspend_result: dict = {}
    suspend = threading.Thread(target=lambda: suspend_result.setdefault(
        "response", owner.request("POST", f"/api/workspaces/{team['id']}/suspend", {}, key=f"failure-{error_type}-suspend"),
    ))
    suspend.start()
    assert platform_committed.wait(3)

    failure_done = threading.Event()
    failure = threading.Thread(target=lambda: (
        service._finalize_remote_failure(claimed, error_type, "injected failure", retry=retry),
        failure_done.set(),
    ))
    failure.start()
    assert not failure_done.wait(0.1)
    release_reconcile.set()
    suspend.join(timeout=3)
    failure.join(timeout=3)

    assert suspend_result["response"][0] == 200
    assert failure_done.is_set()
    assert service.state.get_task(task["id"])["status"] == "paused"
    assert service.state.raw_classification_plan(
        payload["raw_path"], payload["raw_revision"], payload["taxonomy_revision"],
    )["status"] == "paused"
    assert owner.request("POST", f"/api/workspaces/{team['id']}/restore", {}, key=f"failure-{error_type}-restore")[0] == 200
    assert StateStore(service.root, recover_running=False).get_task(task["id"])["status"] == "queued"


def test_startup_reconciles_platform_workspace_status(lifecycle_server):
    app, owner, _member, team, _owner_personal, _member_personal = _team_world(lifecycle_server)
    service = app.workspace_service(context_for(app, owner))
    root = service.root
    task, _ = service.state.enqueue_task("supplement", "crash", {"path": "concepts/missing.md"})
    # Simulate a crash after the authoritative Platform transaction committed but before StateStore was projected.
    with app.platform.connect() as db:
        db.execute("UPDATE workspaces SET status='suspended' WHERE id=?", (team["id"],))
    app.close()

    restarted = create_app(app.project_root, app.viewer_dir, start_worker=False, multi_user=True)
    try:
        assert StateStore(root, recover_running=False).get_task(task["id"])["status"] == "paused"
    finally:
        restarted.close()


def test_deleted_team_does_not_block_sole_owner_account_deletion(lifecycle_server):
    app, base = lifecycle_server
    owner = Client(base)
    owner.register("deleted-owner@example.com", "Deleted Owner")
    team = _create_team(owner, "Deleted Owner Team", key="deleted-owner-team")
    owner.request("POST", "/api/workspaces/switch", {"workspace_id": team["id"]}, key="deleted-owner-switch")
    context = context_for(app, owner)
    service = app.workspace_service(context)
    root = service.root
    article_id = "a" * 32
    markdown = (
        f"# Retained publication\n\n> Article-ID: {article_id}\n> Category-ID: " + "b" * 32
        + "\n> Classification: confirmed\n> Category: concepts\n> Status: 词条\n\nRetained body.\n"
    )
    article_path = root / "wiki" / "concepts" / "retained.md"
    article_path.parent.mkdir(parents=True, exist_ok=True)
    article_path.write_text(markdown, encoding="utf-8")
    app.platform.save_model(team["id"], "openai-compatible", "https://example.com/v1", "secret", "model")
    snapshot = {
        "title": "Retained publication", "category": "concepts", "content_status": "词条",
        "markdown": public_markdown(markdown), "summary": "Retained body.",
        "attribution": "Deleted Owner", "source_summaries": [],
    }
    preview = app.platform.create_preview(
        context, "concepts/retained.md", "revision", article_id, snapshot_fingerprint(snapshot), snapshot,
    )
    submission = app.platform.submit_preview(context, preview["preview_id"])
    app.platform.ai_decide(submission["id"], "pass", {"summary": "accepted"})
    approved = app.platform.admin_decide(context, submission["id"], "approve", "approved")
    with app.platform.connect() as db:
        before_submission = db.execute("SELECT snapshot_json,content_hash FROM submissions WHERE id=?", (submission["id"],)).fetchone()
        before_revision = db.execute(
            "SELECT snapshot_json,content_hash FROM public_revisions WHERE entry_id=?",
            (approved["public_entry_id"],),
        ).fetchone()

    assert owner.request("POST", f"/api/workspaces/{team['id']}/suspend", {}, key="deleted-owner-suspend")[0] == 200
    assert owner.request("POST", f"/api/workspaces/{team['id']}/delete", {}, key="deleted-owner-delete")[0] == 200
    deleted = owner.request("POST", "/api/account/delete", {"password": "correct-horse-123"}, key="deleted-owner-account")
    assert deleted == (200, {"deleted": True})
    assert root.is_dir() and article_path.read_bytes() == markdown.encode("utf-8")
    assert app.platform.load_model(team["id"])["model"] == "model"
    with app.platform.connect() as db:
        workspace = db.execute("SELECT status,owner_id FROM workspaces WHERE id=?", (team["id"],)).fetchone()
        user = db.execute("SELECT status FROM users WHERE id=?", (context.user_id,)).fetchone()
        after_submission = db.execute("SELECT snapshot_json,content_hash FROM submissions WHERE id=?", (submission["id"],)).fetchone()
        after_revision = db.execute(
            "SELECT snapshot_json,content_hash FROM public_revisions WHERE entry_id=?",
            (approved["public_entry_id"],),
        ).fetchone()
        transfers = db.execute(
            "SELECT COUNT(*) FROM audit_events WHERE action IN ('workspace.owner_transfer','organization.owner_transfer') AND actor_id=?",
            (context.user_id,),
        ).fetchone()[0]
    assert tuple(workspace) == ("deleted", context.user_id)
    assert user["status"] == "deleted"
    assert tuple(after_submission) == tuple(before_submission)
    assert tuple(after_revision) == tuple(before_revision)
    assert transfers == 0
