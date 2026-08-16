from __future__ import annotations

import threading
from pathlib import Path

import pytest

from serve import create_app, create_server
from state_store import StateStore
from tests.test_multitenant import Client, context_for
from tests.test_team_collaboration import _create_team, _invite_and_accept


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
