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


def test_ingest_preview_does_not_enqueue_classification(lifecycle_server):
    app, owner, _member, _team, _owner_personal, _member_personal = _team_world(lifecycle_server)
    service = app.workspace_service(context_for(app, owner))
    raw_path = service.root / "raw" / "local" / "actor-preview.md"
    raw_path.parent.mkdir(parents=True, exist_ok=True)
    raw_path.write_text("# Actor Preview\n\nRaw body.\n", encoding="utf-8")
    before_task_ids = {item["id"] for item in service.state.list_tasks()}

    response = owner.request("GET", "/api/ingest/preview?path=raw/local/actor-preview.md")

    assert response[0] == 200
    assert "classification_plan" not in response[1]
    assert {item["id"] for item in service.state.list_tasks()} == before_task_ids


def test_cross_workspace_service_creation_does_not_invert_platform_and_cache_locks(lifecycle_server, monkeypatch):
    app, base = lifecycle_server
    owner = Client(base)
    owner.register("lock-order@example.com", "Lock Order")
    team_a = _create_team(owner, "Lock A", key="lock-team-a")
    team_b = _create_team(owner, "Lock B", key="lock-team-b")
    owner.request("POST", "/api/workspaces/switch", {"workspace_id": team_a["id"]}, key="lock-switch-a")
    context_a = context_for(app, owner)
    service_a = app.workspace_service(context_a)
    owner.request("POST", "/api/workspaces/switch", {"workspace_id": team_b["id"]}, key="lock-switch-b")
    context_b = context_for(app, owner)
    owner.request("POST", "/api/workspaces/switch", {"workspace_id": team_a["id"]}, key="lock-switch-a-again")
    with app._service_lock:
        warmed_b = app._services.pop(team_b["id"], None)
        app._diagnostics.pop(team_b["id"], None)
        app._publication_backfills = {
            key for key in app._publication_backfills if key[0] != team_b["id"]
        }
    if warmed_b is not None:
        warmed_b.close()

    original_authorize = app.platform.authorize_workspace
    original_backfill = app.platform.backfill_workspace_publication_sources
    original_action = app.platform.authorized_workspace_action
    b_authorized = threading.Event()
    release_b = threading.Event()
    a_has_platform = threading.Event()
    b_in_backfill = threading.Event()
    release_backfill = threading.Event()

    def delayed_authorize(user_id, workspace_id, permission):
        result = original_authorize(user_id, workspace_id, permission)
        if threading.current_thread().name == "workspace-b" and workspace_id == team_b["id"]:
            b_authorized.set()
            assert release_b.wait(10)
        return result

    def delayed_backfill(context, articles):
        if threading.current_thread().name == "workspace-b" and context.workspace_id == team_b["id"]:
            b_in_backfill.set()
            assert release_backfill.wait(10)
        return original_backfill(context, articles)

    @contextlib.contextmanager
    def delayed_platform_action(user_id, workspace_id, permission):
        with original_action(user_id, workspace_id, permission):
            if threading.current_thread().name == "workspace-a" and workspace_id == team_a["id"]:
                a_has_platform.set()
                release_backfill.set()
            yield

    monkeypatch.setattr(app.platform, "authorize_workspace", delayed_authorize)
    monkeypatch.setattr(app.platform, "backfill_workspace_publication_sources", delayed_backfill)
    monkeypatch.setattr(app.platform, "authorized_workspace_action", delayed_platform_action)
    outcomes: dict = {}

    def create_b():
        try:
            outcomes["b"] = app.workspace_service(context_b)
        except Exception as exc:
            outcomes["b_error"] = exc

    def write_action_a():
        action = app.workspace_action(context_a, service_a, "wiki.write")
        try:
            diagnostics = action.__enter__()
            if diagnostics is None:
                diagnostics = app.diagnostics_for(context_a, service_a)
            outcomes["a"] = diagnostics
        except Exception as exc:
            outcomes["a_error"] = exc
        finally:
            action.__exit__(None, None, None)

    b_thread = threading.Thread(target=create_b, name="workspace-b", daemon=True)
    a_thread = threading.Thread(target=write_action_a, name="workspace-a", daemon=True)
    b_thread.start()
    assert b_authorized.wait(10)
    release_b.set()
    assert b_in_backfill.wait(10)
    a_thread.start()
    assert a_has_platform.wait(10)
    a_thread.join(timeout=10)
    b_thread.join(timeout=10)

    assert not a_thread.is_alive()
    assert not b_thread.is_alive()
    assert outcomes.get("a_error") is None
    assert outcomes.get("b_error") is None
    assert outcomes["a"] is app._diagnostics[team_a["id"]]
    assert outcomes["b"] is app._services[team_b["id"]]


def test_long_workspace_read_does_not_hold_global_platform_lock(lifecycle_server):
    app, owner, _member, team, _owner_personal, _member_personal = _team_world(lifecycle_server)
    context = context_for(app, owner)
    service = app.workspace_service(context)
    read_entered = threading.Event()
    release_read = threading.Event()

    def long_read():
        with app.workspace_action(context, service, "wiki.read"):
            read_entered.set()
            assert release_read.wait(3)

    read_thread = threading.Thread(target=long_read)
    read_thread.start()
    assert read_entered.wait(3)
    platform_done = threading.Event()
    platform_thread = threading.Thread(target=lambda: (
        app.platform.workspace_summary_for_user(context.user_id, team["id"]),
        platform_done.set(),
    ))
    platform_thread.start()

    assert platform_done.wait(0.5)
    release_read.set()
    read_thread.join(timeout=3)
    platform_thread.join(timeout=3)
    assert not read_thread.is_alive()
    assert not platform_thread.is_alive()


def test_member_removal_waits_for_admitted_private_read(lifecycle_server):
    app, owner, member, team, _owner_personal, _member_personal = _team_world(lifecycle_server)
    owner_context = context_for(app, owner)
    member_context = context_for(app, member)
    member_id = context_for(app, member).user_id
    assert owner.request(
        "POST", f"/api/workspace/members/{member_id}/role", {"role": "viewer"}, key="read-viewer",
    )[0] == 200
    service = app.workspace_service(member_context)
    article = service.root / "wiki" / "concepts" / "secret.md"
    article.parent.mkdir(parents=True, exist_ok=True)
    article.write_text("# Secret\n\nSecret body\n", encoding="utf-8")

    read_authorized = threading.Event()
    release_read = threading.Event()
    mutation_waiting = threading.Event()
    outcomes: dict = {}

    def read_article():
        with app.workspace_action(member_context, service, "wiki.read"):
            read_authorized.set()
            assert release_read.wait(5)
            outcomes["read"] = service.read_article("concepts/secret.md")

    read_thread = threading.Thread(target=read_article)
    remove_done = threading.Event()

    def remove_member():
        mutation_waiting.set()
        outcomes["remove"] = app.run_workspace_access_mutation(
            team["id"], lambda: app.platform.remove_workspace_member(owner_context, member_id),
        )
        remove_done.set()

    remove_thread = threading.Thread(target=remove_member)
    read_thread.start()
    assert read_authorized.wait(5)
    remove_thread.start()
    assert mutation_waiting.wait(5)
    assert not remove_done.wait(0.2)
    release_read.set()
    read_thread.join(timeout=5)
    remove_thread.join(timeout=5)

    assert "Secret body" in outcomes["read"]["markdown"]
    assert outcomes["remove"]["status"] == "suspended"
    assert member.request("GET", "/api/article?path=concepts/secret.md")[0] == 401


def test_account_deletion_waits_for_admitted_read_and_evicts_cache(lifecycle_server, monkeypatch):
    app, base = lifecycle_server
    owner = Client(base)
    owner.register("delete-read@example.com", "Delete Read")
    context = context_for(app, owner)
    service = app.workspace_service(context)
    article = service.root / "wiki" / "concepts" / "delete-secret.md"
    article.parent.mkdir(parents=True, exist_ok=True)
    article.write_text("# Delete Secret\n\nSecret body\n", encoding="utf-8")

    original_authorize = app.platform.authorize_workspace
    read_authorized = threading.Event()
    release_read = threading.Event()
    read_checks = 0

    def delayed_read(user_id, workspace_id, permission):
        nonlocal read_checks
        result = original_authorize(user_id, workspace_id, permission)
        if user_id == context.user_id and workspace_id == context.workspace_id and permission == "wiki.read":
            read_checks += 1
            if read_checks == 2:
                read_authorized.set()
                assert release_read.wait(5)
        return result

    monkeypatch.setattr(app.platform, "authorize_workspace", delayed_read)
    outcomes: dict = {}
    read_thread = threading.Thread(
        target=lambda: outcomes.setdefault(
            "read", owner.request("GET", "/api/article?path=concepts/delete-secret.md"),
        ),
    )
    delete_done = threading.Event()

    def delete_owner():
        app.delete_account(context, "correct-horse-123")
        outcomes["deleted"] = True
        delete_done.set()

    delete_thread = threading.Thread(target=delete_owner)
    read_thread.start()
    assert read_authorized.wait(5)
    delete_thread.start()
    assert not delete_done.wait(0.2)
    assert service.root.exists()
    release_read.set()
    read_thread.join(timeout=5)
    delete_thread.join(timeout=5)

    assert outcomes["read"][0] == 200
    assert outcomes["deleted"] is True
    assert not service.root.exists()
    assert service.retired
    assert context.workspace_id not in app._services
    assert context.workspace_id not in app._diagnostics
    assert all(key[0] != context.workspace_id for key in app._publication_backfills)


def test_account_deletion_evicts_service_created_after_authorization(lifecycle_server, monkeypatch):
    app, base = lifecycle_server
    owner = Client(base)
    owner.register("delete-create@example.com", "Delete Create")
    context = context_for(app, owner)
    with app._service_lock:
        prior = app._services.pop(context.workspace_id, None)
        app._diagnostics.pop(context.workspace_id, None)
        app._publication_backfills = {
            key for key in app._publication_backfills if key[0] != context.workspace_id
        }
    if prior is not None:
        prior.close()

    original_authorize = app.platform.authorize_workspace
    service_authorized = threading.Event()
    release_service = threading.Event()

    def delayed_service_authorization(user_id, workspace_id, permission):
        result = original_authorize(user_id, workspace_id, permission)
        if threading.current_thread().name == "service-create" and workspace_id == context.workspace_id:
            service_authorized.set()
            assert release_service.wait(5)
        return result

    monkeypatch.setattr(app.platform, "authorize_workspace", delayed_service_authorization)
    outcomes: dict = {}

    def create_service():
        outcomes["service"] = app.workspace_service(context)

    def delete_owner():
        app.delete_account(context, "correct-horse-123")
        outcomes["deleted"] = True

    create_thread = threading.Thread(target=create_service, name="service-create")
    delete_thread = threading.Thread(target=delete_owner, name="account-delete")
    create_thread.start()
    assert service_authorized.wait(5)
    delete_thread.start()
    assert delete_thread.is_alive()
    release_service.set()
    create_thread.join(timeout=5)
    delete_thread.join(timeout=5)

    assert outcomes["deleted"] is True
    assert outcomes["service"].retired
    assert not outcomes["service"].root.exists()
    assert context.workspace_id not in app._services
    assert context.workspace_id not in app._diagnostics
    assert all(key[0] != context.workspace_id for key in app._publication_backfills)


@pytest.mark.parametrize("permission", ["wiki.read", "wiki.write"])
def test_account_deletion_retries_when_invitation_expands_workspace_set(
    lifecycle_server, monkeypatch, permission,
):
    app, base = lifecycle_server
    owner, victim = Client(base), Client(base)
    owner.register("expansion-owner@example.com", "Expansion Owner")
    victim.register("expansion-victim@example.com", "Expansion Victim")
    victim_context = context_for(app, victim)
    victim_personal_root = app.platform.workspace_root(victim_context.workspace_root_name)
    team = _create_team(owner, "Expansion Team", key=f"expansion-team-{permission}")
    assert owner.request(
        "POST", "/api/workspaces/switch", {"workspace_id": team["id"]}, key=f"expansion-owner-{permission}",
    )[0] == 200
    team_service = app.workspace_service(context_for(app, owner))
    article_path = team_service.root / "wiki" / "concepts" / "private.md"
    article_path.parent.mkdir(parents=True, exist_ok=True)
    article_path.write_text("# Private\n\nPrivate body\n", encoding="utf-8")
    invitation = owner.request(
        "POST", "/api/workspace/invitations",
        {"email": "expansion-victim@example.com", "role": "editor"},
        key=f"expansion-invite-{permission}",
    )[1]

    original_workspace_ids = app.platform.account_workspace_ids
    initial_enumeration = threading.Event()
    release_deletion = threading.Event()

    def delayed_workspace_ids(user_id):
        workspace_ids = original_workspace_ids(user_id)
        if threading.current_thread().name == "account-delete":
            initial_enumeration.set()
            assert release_deletion.wait(5)
        return workspace_ids

    monkeypatch.setattr(app.platform, "account_workspace_ids", delayed_workspace_ids)
    outcomes: dict = {}
    delete_done = threading.Event()

    def delete_victim():
        app.delete_account(victim_context, "correct-horse-123")
        outcomes["deleted"] = True
        delete_done.set()

    delete_thread = threading.Thread(target=delete_victim, name="account-delete")
    delete_thread.start()
    assert initial_enumeration.wait(5)
    assert victim.request(
        "POST", f"/api/invitations/{invitation['id']}/accept", {},
        key=f"expansion-accept-{permission}",
    )[0] == 200
    assert victim.request(
        "POST", "/api/workspaces/switch", {"workspace_id": team["id"]},
        key=f"expansion-switch-{permission}",
    )[0] == 200
    expanded_context = context_for(app, victim)
    expanded_service = app.workspace_service(expanded_context)
    before = expanded_service.read_article("concepts/private.md")
    request_admitted = threading.Event()
    release_request = threading.Event()

    def workspace_request():
        with app.workspace_action(expanded_context, expanded_service, permission):
            request_admitted.set()
            assert release_request.wait(5)
            if permission == "wiki.read":
                outcomes["request"] = expanded_service.read_article("concepts/private.md")
            else:
                updated = before["markdown"] + "\nUpdated before deletion.\n"
                outcomes["request"] = expanded_service.files.commit(
                    {"wiki/concepts/private.md": updated.encode("utf-8")},
                    kind="test-expanded-workspace-write",
                    operation_id="test-expanded-workspace-write",
                )

    request_thread = threading.Thread(target=workspace_request, name="expanded-workspace-request")
    request_thread.start()
    assert request_admitted.wait(5)
    release_deletion.set()
    assert not delete_done.wait(0.2)
    release_request.set()
    request_thread.join(timeout=5)
    delete_thread.join(timeout=5)

    assert outcomes["deleted"] is True
    if permission == "wiki.read":
        assert "Private body" in outcomes["request"]["markdown"]
    else:
        assert outcomes["request"]["status"] == "committed"
        assert "Updated before deletion." in article_path.read_text(encoding="utf-8")
    assert not victim_personal_root.exists()
    assert app.platform.workspace_storage_state(team["id"])["status"] == "active"
    assert app._services[team["id"]] is expanded_service
    assert not expanded_service.retired


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


def test_lifecycle_replay_uses_current_membership_and_permissions(lifecycle_server):
    app, owner, member, team, _owner_personal, _member_personal = _team_world(lifecycle_server)
    owner_id = context_for(app, owner).user_id
    member_id = context_for(app, member).user_id
    assert owner.request("POST", f"/api/workspaces/{team['id']}/suspend", {}, key="identity-suspend")[0] == 200
    assert owner.request("POST", f"/api/workspaces/{team['id']}/restore", {}, key="identity-restore")[0] == 200
    for client, key in ((owner, "identity-owner-switch"), (member, "identity-member-switch")):
        assert client.request("POST", "/api/workspaces/switch", {"workspace_id": team["id"]}, key=key)[0] == 200
    assert owner.request(
        "POST", "/api/workspace/owner-transfer", {"user_id": member_id}, key="identity-transfer",
    )[0] == 200
    assert member.request(
        "POST", "/api/workspace/rename", {"display_name": "Current Identity Team"}, key="identity-rename",
    )[0] == 200

    replay = owner.request("POST", f"/api/workspaces/{team['id']}/suspend", {}, key="identity-suspend")
    assert replay == (200, app.platform.workspace_summary_for_user(owner_id, team["id"]))
    assert replay[1]["role"] == "editor"
    assert replay[1]["display_name"] == "Current Identity Team"
    assert replay[1]["can_suspend"] is False
    assert replay[1]["can_leave"] is True

    assert member.request(
        "POST", f"/api/workspace/members/{owner_id}/role", {"role": "viewer"}, key="identity-viewer",
    )[0] == 200
    downgraded = owner.request("POST", f"/api/workspaces/{team['id']}/suspend", {}, key="identity-suspend")
    assert downgraded == (200, app.platform.workspace_summary_for_user(owner_id, team["id"]))
    assert downgraded[1]["role"] == "viewer"
    assert downgraded[1]["permissions"] == ["wiki.read"]

    assert member.request(
        "POST", f"/api/workspace/members/{owner_id}/remove", {}, key="identity-remove",
    )[0] == 200
    owner.request("POST", "/api/auth/login", {
        "email": "lifecycle-owner@example.com", "password": "correct-horse-123",
    }, key="identity-login")
    removed = owner.request("POST", f"/api/workspaces/{team['id']}/suspend", {}, key="identity-suspend")
    assert removed[0] == 404
    assert app.platform.workspace_storage_state(team["id"])["status"] == "active"


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
    payload = {"path": "concepts/missing.md"}
    task, _ = service.state.enqueue_task(
        "supplement", "failure-task", payload, actor_user_id=context.user_id,
    )
    claimed = service.state.claim_task({"supplement"})
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
