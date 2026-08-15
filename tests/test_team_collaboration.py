from __future__ import annotations

from datetime import datetime, timedelta, timezone
import threading
from pathlib import Path

import pytest

import dynamic_categories as dc
from publication import public_markdown, snapshot_fingerprint
from serve import create_app, create_server
from tests.test_multitenant import Client, context_for


@pytest.fixture
def team_server(tmp_path: Path):
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


def _write_article(app, workspace_id: str, title: str) -> None:
    with app.platform.connect() as db:
        owner_id = db.execute(
            "SELECT owner_id FROM workspaces WHERE id=?", (workspace_id,),
        ).fetchone()[0]
    workspace = app.platform.authorize_workspace(owner_id, workspace_id, "wiki.read")
    path = app.platform.workspace_root(workspace["root_name"]) / "wiki" / "concepts" / "shared.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f"# {title}\n\n> Category: concepts\n> Status: 词条\n\n## 它做什么\n\n{title}\n",
        encoding="utf-8",
    )


def _create_team(client: Client, name: str, *, key: str = "create-team") -> dict:
    status, team = client.request("POST", "/api/workspaces", {"display_name": name}, key=key)
    assert status == 201
    return team


def _invite_and_accept(owner: Client, member: Client, email: str, role: str = "editor") -> tuple[dict, dict]:
    status, invitation = owner.request(
        "POST", "/api/workspace/invitations", {"email": email, "role": role}, key=f"invite-{email}",
    )
    assert status == 201
    pending = member.request("GET", "/api/invitations")[1]
    assert [item["id"] for item in pending] == [invitation["id"]]
    status, accepted = member.request(
        "POST", f"/api/invitations/{invitation['id']}/accept", {}, key=f"accept-{invitation['id']}",
    )
    assert status == 200 and accepted["status"] == "accepted"
    return invitation, accepted


def test_workspace_switch_is_isolated_per_session(team_server):
    app, base = team_server
    first, second = Client(base), Client(base)
    first.register("sessions@example.com", "Sessions")
    personal = context_for(app, first)
    team = _create_team(first, "Research Team")
    assert second.request(
        "POST", "/api/auth/login", {"email": "sessions@example.com", "password": "correct-horse-123"},
        key="login-second",
    )[0] == 200
    _write_article(app, personal.workspace_id, "Personal article")
    _write_article(app, team["id"], "Team article")

    switched = first.request(
        "POST", "/api/workspaces/switch", {"workspace_id": team["id"]}, key="switch-team",
    )
    assert switched[0] == 200 and switched[1]["workspace"]["id"] == team["id"]
    assert context_for(app, first).workspace_id == team["id"]
    assert context_for(app, second).workspace_id == personal.workspace_id
    assert first.request("GET", "/api/articles")[1][0]["title"] == "Team article"
    assert second.request("GET", "/api/articles")[1][0]["title"] == "Personal article"

    reopened = type(app.platform)(app.project_root)
    assert reopened.resolve_session(first.cookie.split("=", 1)[1]).workspace_id == team["id"]
    assert reopened.resolve_session(second.cookie.split("=", 1)[1]).workspace_id == personal.workspace_id


def test_legacy_publication_backfill_runs_for_each_author_in_cached_team_service(team_server):
    app, base = team_server
    owner, member = Client(base), Client(base)
    owner.register("legacy-owner@example.com", "Legacy Owner")
    member.register("legacy-member@example.com", "Legacy Member")
    team = _create_team(owner, "Legacy Team")
    owner.request("POST", "/api/workspaces/switch", {"workspace_id": team["id"]}, key="owner-switch-legacy")
    _invite_and_accept(owner, member, "legacy-member@example.com", "editor")
    member.request("POST", "/api/workspaces/switch", {"workspace_id": team["id"]}, key="member-switch-legacy")
    _write_article(app, team["id"], "Legacy shared article")

    owner_context = context_for(app, owner)
    owner_service = app.workspace_service(owner_context)
    value = owner_service.read_article("concepts/shared.md")
    if not value["article_id"]:
        article_path = owner_service.root / "wiki" / value["path"]
        article_path.write_text(
            dc.ensure_article_metadata(value["markdown"], category_id=None, status="pending"),
            encoding="utf-8",
        )
        value = owner_service.read_article("concepts/shared.md")

    member_context = context_for(app, member)
    public_snapshot = {
        "title": value["title"],
        "category": value["category"],
        "content_status": value["content_status"],
        "markdown": public_markdown(value["markdown"]),
        "summary": "summary",
        "attribution": "Legacy Member",
        "source_summaries": [],
    }
    preview = app.platform.create_preview(
        member_context,
        value["path"],
        value["revision"],
        value["article_id"],
        snapshot_fingerprint(public_snapshot),
        public_snapshot,
    )
    submission = app.platform.submit_preview(member_context, preview["preview_id"])
    app.platform.ai_decide(submission["id"], "pass", {"summary": "accepted"})
    approved = app.platform.admin_decide(owner_context, submission["id"], "approve", "approved")
    with app.platform.connect() as db:
        db.execute(
            "UPDATE public_entries SET source_workspace_id=NULL,source_article_id=NULL WHERE id=?",
            (approved["public_entry_id"],),
        )
        db.execute(
            "UPDATE submissions SET article_id=NULL WHERE id=?",
            (submission["id"],),
        )

    assert app.platform.article_publication(member_context, value)["state"] == "not_published"
    assert app.workspace_service(member_context) is owner_service
    publication = app.platform.article_publication(member_context, value)
    assert publication["state"] == "published"
    assert publication["public_entry_id"] == approved["public_entry_id"]


def test_invitation_role_changes_and_removal_take_effect_immediately(team_server):
    app, base = team_server
    owner, member = Client(base), Client(base)
    owner.register("team-owner@example.com", "Team Owner")
    member.register("team-member@example.com", "Team Member")
    team = _create_team(owner, "Product Team")
    owner.request("POST", "/api/workspaces/switch", {"workspace_id": team["id"]}, key="owner-switch")
    _invite_and_accept(owner, member, "team-member@example.com", "editor")
    assert member.request(
        "POST", "/api/workspaces/switch", {"workspace_id": team["id"]}, key="member-switch",
    )[0] == 200
    member_id = context_for(app, member).user_id

    members = owner.request("GET", "/api/workspace/members")[1]
    assert {row["role"] for row in members} == {"owner", "editor"}
    changed = owner.request(
        "POST", f"/api/workspace/members/{member_id}/role", {"role": "viewer"}, key="make-viewer",
    )
    assert changed[0] == 200 and changed[1]["role"] == "viewer"
    assert member.request("GET", "/api/articles")[0] == 200
    assert member.request("GET", "/api/workspace/members")[0] == 403
    assert member.request("POST", "/api/governance", {}, key="viewer-write")[0] == 403

    assert owner.request(
        "POST", f"/api/workspace/members/{member_id}/role", {"role": "editor"}, key="make-editor",
    )[0] == 200
    editor_write = member.request("POST", "/api/governance", {}, key="editor-write")
    assert editor_write == (422, {"error": "AI governance requires a configured model"})
    assert owner.request(
        "POST", f"/api/workspace/members/{member_id}/remove", {}, key="remove-member",
    )[0] == 200
    assert member.request("GET", "/api/articles")[0] == 401
    with app.platform.connect() as db:
        assert db.execute(
            "SELECT status FROM workspace_members WHERE workspace_id=? AND user_id=?",
            (team["id"], member_id),
        ).fetchone()[0] == "suspended"


def test_owner_transfer_preserves_invariants_and_permissions(team_server):
    app, base = team_server
    owner, successor = Client(base), Client(base)
    owner.register("transfer-owner@example.com", "Transfer Owner")
    successor.register("transfer-successor@example.com", "Transfer Successor")
    owner_id = context_for(app, owner).user_id
    successor_id = context_for(app, successor).user_id
    team = _create_team(owner, "Transfer Team")
    owner.request("POST", "/api/workspaces/switch", {"workspace_id": team["id"]}, key="switch-owner")
    _invite_and_accept(owner, successor, "transfer-successor@example.com", "viewer")
    successor.request("POST", "/api/workspaces/switch", {"workspace_id": team["id"]}, key="switch-successor")

    assert owner.request(
        "POST", f"/api/workspace/members/{owner_id}/remove", {}, key="remove-last-owner",
    )[0] == 422
    transferred = owner.request(
        "POST", "/api/workspace/owner-transfer", {"user_id": successor_id}, key="transfer-owner",
    )
    assert transferred[0] == 200 and transferred[1]["new_owner_id"] == successor_id
    assert owner.request(
        "POST", "/api/workspace/rename", {"display_name": "Old owner cannot rename"}, key="old-rename",
    )[0] == 403
    assert successor.request(
        "POST", "/api/workspace/rename", {"display_name": "New Owner Team"}, key="new-rename",
    )[0] == 200
    with app.platform.connect() as db:
        workspace = db.execute("SELECT owner_id,display_name FROM workspaces WHERE id=?", (team["id"],)).fetchone()
        roles = dict(db.execute(
            "SELECT user_id,role FROM workspace_members WHERE workspace_id=?", (team["id"],),
        ).fetchall())
        assert workspace["owner_id"] == successor_id
        assert workspace["display_name"] == "New Owner Team"
        assert roles == {owner_id: "editor", successor_id: "owner"}
        assert db.execute("PRAGMA foreign_key_check").fetchall() == []


def test_platform_idempotency_and_switch_idor(team_server):
    app, base = team_server
    owner, outsider = Client(base), Client(base)
    owner.register("idempotent-owner@example.com", "Owner")
    outsider.register("idempotent-outsider@example.com", "Outsider")
    first = owner.request("POST", "/api/workspaces", {"display_name": "One Team"}, key="same-create")
    replay = owner.request("POST", "/api/workspaces", {"display_name": "One Team"}, key="same-create")
    conflict = owner.request("POST", "/api/workspaces", {"display_name": "Different Team"}, key="same-create")
    assert first[0] == 201 and replay[0] == 200 and replay[1]["id"] == first[1]["id"]
    assert conflict[0] == 409
    assert outsider.request(
        "POST", "/api/workspaces/switch", {"workspace_id": first[1]["id"]}, key="cross-switch",
    )[0] == 404
    assert outsider.request("GET", "/api/workspace/members")[0] == 200
    with app.platform.connect() as db:
        assert db.execute(
            "SELECT COUNT(*) FROM workspaces WHERE owner_id=? AND display_name='One Team'",
            (context_for(app, owner).user_id,),
        ).fetchone()[0] == 1


def test_platform_idempotency_is_scoped_by_session_and_workspace(team_server):
    app, base = team_server
    first, second = Client(base), Client(base)
    first.register("scoped-idempotency@example.com", "Scoped User")
    personal = context_for(app, first)
    team_a = _create_team(first, "Team A", key="create-a")
    team_b = _create_team(first, "Team B", key="create-b")
    assert second.request(
        "POST", "/api/auth/login",
        {"email": "scoped-idempotency@example.com", "password": "correct-horse-123"},
        key="login-second-session",
    )[0] == 200

    first_switch = first.request(
        "POST", "/api/workspaces/switch", {"workspace_id": team_a["id"]}, key="shared-switch-key",
    )
    second_switch = second.request(
        "POST", "/api/workspaces/switch", {"workspace_id": team_b["id"]}, key="shared-switch-key",
    )
    assert first_switch[1]["workspace"]["id"] == team_a["id"]
    assert second_switch[1]["workspace"]["id"] == team_b["id"]
    assert first_switch[1]["csrf_token"] != second_switch[1]["csrf_token"]
    assert context_for(app, first).workspace_id == team_a["id"]
    assert context_for(app, second).workspace_id == team_b["id"]

    assert first.request(
        "POST", "/api/workspace/rename", {"display_name": "Same visible name"}, key="same-rename-key",
    )[0] == 200
    assert second.request(
        "POST", "/api/workspace/rename", {"display_name": "Same visible name"}, key="same-rename-key",
    )[0] == 200
    with app.platform.connect() as db:
        names = dict(db.execute(
            "SELECT id,display_name FROM workspaces WHERE id IN (?,?)", (team_a["id"], team_b["id"]),
        ).fetchall())
    assert names == {team_a["id"]: "Same visible name", team_b["id"]: "Same visible name"}
    assert personal.workspace_id not in names


def test_platform_idempotency_rolls_back_business_change_with_claim(team_server):
    app, base = team_server
    owner = Client(base)
    owner.register("atomic-idempotency@example.com", "Atomic User")
    context = context_for(app, owner)
    endpoint = "/api/workspace/rename"
    payload = {"display_name": "Atomic rename"}

    def interrupted_action():
        with app.platform.connect() as db:
            db.execute("UPDATE workspaces SET display_name=? WHERE id=?", ("must rollback", context.workspace_id))
            db.commit()
        raise RuntimeError("simulated process interruption")

    with pytest.raises(RuntimeError, match="simulated process interruption"):
        app.platform.run_platform_idempotent(
            f"workspace:{context.workspace_id}", endpoint, "atomic-key", payload, interrupted_action,
        )
    with app.platform.connect() as db:
        assert db.execute("SELECT display_name FROM workspaces WHERE id=?", (context.workspace_id,)).fetchone()[0] != "must rollback"
        assert db.execute(
            "SELECT 1 FROM platform_idempotency WHERE scope=? AND endpoint=? AND key=?",
            (f"workspace:{context.workspace_id}", endpoint, "atomic-key"),
        ).fetchone() is None

    response, replay = app.platform.run_platform_idempotent(
        f"workspace:{context.workspace_id}", endpoint, "atomic-key", payload,
        lambda: app.platform.rename_workspace(context, "Atomic rename"),
    )
    assert replay is False and response["display_name"] == "Atomic rename"
    replayed, replay = app.platform.run_platform_idempotent(
        f"workspace:{context.workspace_id}", endpoint, "atomic-key", payload,
        lambda: pytest.fail("replayed action must not run"),
    )
    assert replay is True and replayed == response


def test_create_team_removes_root_when_outer_transaction_rolls_back(team_server):
    app, base = team_server
    owner = Client(base)
    owner.register("team-rollback@example.com", "Rollback Owner")
    context = context_for(app, owner)
    before_roots = {path.name for path in app.platform.spaces_root.iterdir()}

    def create_then_fail_serialization():
        return {"team": app.platform.create_team(context, "Rolled back team"), "invalid": object()}

    with pytest.raises(TypeError):
        app.platform.run_platform_idempotent(
            f"user:{context.user_id}", "/api/workspaces", "rollback-team", {"display_name": "Rolled back team"},
            create_then_fail_serialization,
        )

    assert {path.name for path in app.platform.spaces_root.iterdir()} == before_roots
    with app.platform.connect() as db:
        assert db.execute("SELECT 1 FROM workspaces WHERE display_name='Rolled back team'").fetchone() is None
        assert db.execute("SELECT 1 FROM organizations WHERE display_name='Rolled back team'").fetchone() is None


def test_create_team_removes_partial_root_when_raw_directory_creation_fails(team_server, monkeypatch):
    app, base = team_server
    owner = Client(base)
    owner.register("team-mkdir@example.com", "Mkdir Owner")
    context = context_for(app, owner)
    before_roots = {path.name for path in app.platform.spaces_root.iterdir()}
    real_mkdir = Path.mkdir

    def fail_raw(path: Path, *args, **kwargs):
        if path.name == "raw" and path.parent.parent == app.platform.spaces_root:
            raise OSError("simulated raw mkdir failure")
        return real_mkdir(path, *args, **kwargs)

    monkeypatch.setattr(Path, "mkdir", fail_raw)
    with pytest.raises(OSError, match="simulated raw mkdir failure"):
        app.platform.create_team(context, "Partial team")

    assert {path.name for path in app.platform.spaces_root.iterdir()} == before_roots
    with app.platform.connect() as db:
        assert db.execute("SELECT 1 FROM workspaces WHERE display_name='Partial team'").fetchone() is None


def test_workspace_list_excludes_inactive_workspace_or_organization(team_server):
    app, base = team_server
    owner = Client(base)
    owner.register("inactive-team@example.com", "Inactive Owner")
    team = _create_team(owner, "Inactive Team")
    assert team["id"] in {item["id"] for item in owner.request("GET", "/api/workspaces")[1]}

    with app.platform.connect() as db:
        db.execute("UPDATE workspaces SET status='suspended' WHERE id=?", (team["id"],))
    assert team["id"] not in {item["id"] for item in owner.request("GET", "/api/workspaces")[1]}

    with app.platform.connect() as db:
        db.execute("UPDATE workspaces SET status='active' WHERE id=?", (team["id"],))
        db.execute("UPDATE organizations SET status='suspended' WHERE id=?", (team["organization_id"],))
    assert team["id"] not in {item["id"] for item in owner.request("GET", "/api/workspaces")[1]}


def test_expired_invitation_is_closed_and_can_be_reissued(team_server):
    app, base = team_server
    owner, member = Client(base), Client(base)
    owner.register("expiry-owner@example.com", "Expiry Owner")
    member.register("expiry-member@example.com", "Expiry Member")
    team = _create_team(owner, "Expiry Team")
    owner.request("POST", "/api/workspaces/switch", {"workspace_id": team["id"]}, key="expiry-switch")
    first = owner.request(
        "POST", "/api/workspace/invitations",
        {"email": "expiry-member@example.com", "role": "editor"}, key="expiry-first",
    )[1]
    expired = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat(timespec="seconds")
    with app.platform.connect() as db:
        db.execute("UPDATE workspace_invitations SET expires_at=? WHERE id=?", (expired, first["id"]))

    status, payload = member.request(
        "POST", f"/api/invitations/{first['id']}/accept", {}, key="accept-expired",
    )
    assert status == 422 and payload["error"] == "invitation is no longer available"
    with app.platform.connect() as db:
        assert db.execute("SELECT status FROM workspace_invitations WHERE id=?", (first["id"],)).fetchone()[0] == "expired"
    reissued = owner.request(
        "POST", "/api/workspace/invitations",
        {"email": "expiry-member@example.com", "role": "viewer"}, key="expiry-second",
    )
    assert reissued[0] == 201 and reissued[1]["id"] != first["id"]


def test_governance_result_cannot_commit_after_actor_revocation(team_server, monkeypatch):
    app, base = team_server
    owner, member = Client(base), Client(base)
    owner.register("govern-owner@example.com", "Govern Owner")
    member.register("govern-member@example.com", "Govern Member")
    team = _create_team(owner, "Govern Team")
    owner.request("POST", "/api/workspaces/switch", {"workspace_id": team["id"]}, key="govern-owner-switch")
    _invite_and_accept(owner, member, "govern-member@example.com", "editor")
    member.request("POST", "/api/workspaces/switch", {"workspace_id": team["id"]}, key="govern-member-switch")
    member_context = context_for(app, member)
    member_id = member_context.user_id
    _write_article(app, team["id"], "Governed article")
    service = app.workspace_service(member_context)
    article = service.read_article("concepts/shared.md")
    task, _ = service.state.enqueue_task(
        "governance", article["title"], {"path": article["path"], "base_revision": article["revision"]},
        actor_user_id=member_id,
    )
    claimed = service.state.claim_task({"governance"})
    assert claimed and claimed["id"] == task["id"]
    model_started = threading.Event()
    release_model = threading.Event()

    def delayed_model(_article):
        model_started.set()
        assert release_model.wait(3)
        return article["markdown"]

    monkeypatch.setattr(service, "_call_governance_llm", delayed_model)
    monkeypatch.setattr("wiki_ops.article_quality_issues", lambda *_args, **_kwargs: [])
    outcome: dict = {}

    def run_task():
        try:
            outcome["result"] = service._run_governance_task(claimed)
        except Exception as exc:  # capture the worker-visible RemoteError
            outcome["error"] = exc

    thread = threading.Thread(target=run_task)
    thread.start()
    assert model_started.wait(3)
    before = (service.root / "wiki" / "concepts" / "shared.md").read_bytes()
    assert owner.request(
        "POST", f"/api/workspace/members/{member_id}/remove", {}, key="remove-governor",
    )[0] == 200
    release_model.set()
    thread.join(timeout=3)
    assert not thread.is_alive()
    assert getattr(outcome.get("error"), "code", None) == "auth_revoked"
    assert (service.root / "wiki" / "concepts" / "shared.md").read_bytes() == before
    assert list(service.files.history_root.glob(f"govern-{task['id']}-attempt-*")) == []


def test_revoked_task_actor_cannot_finalize(team_server):
    app, base = team_server
    owner, member = Client(base), Client(base)
    owner.register("task-owner@example.com", "Task Owner")
    member.register("task-member@example.com", "Task Member")
    team = _create_team(owner, "Task Team")
    owner.request("POST", "/api/workspaces/switch", {"workspace_id": team["id"]}, key="task-owner-switch")
    _invite_and_accept(owner, member, "task-member@example.com", "editor")
    member.request("POST", "/api/workspaces/switch", {"workspace_id": team["id"]}, key="task-member-switch")
    member_id = context_for(app, member).user_id
    service = app.workspace_service(context_for(app, member))
    task, _ = service.state.enqueue_task(
        "supplement", "revoked-task", {"path": "concepts/missing.md"}, actor_user_id=member_id,
    )
    claimed = service.state.claim_task({"supplement"})
    assert claimed and claimed["id"] == task["id"]

    assert owner.request(
        "POST", f"/api/workspace/members/{member_id}/remove", {}, key="task-remove-member",
    )[0] == 200
    finalized = service._finalize_remote_result(claimed, {"conflict": False})
    assert finalized["status"] == "failed"
    assert finalized["error_type"] == "auth_revoked"
    assert finalized["result"] is None
