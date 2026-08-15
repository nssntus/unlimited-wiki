from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from pathlib import Path

import pytest

from platform_store import PlatformStore, now_iso
from tests.test_multitenant import Client, context_for, seed_article
from serve import create_app, create_server


@pytest.fixture
def membership_server(tmp_path: Path):
    (tmp_path / "viewer" / "dist").mkdir(parents=True)
    (tmp_path / "viewer" / "dist" / "index.html").write_text("<!doctype html><main id='root'></main>", encoding="utf-8")
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


def _set_workspace_role(store: PlatformStore, user_id: str, workspace_id: str, role: str) -> None:
    with store.connect() as db:
        db.execute(
            "UPDATE workspace_members SET role=?,updated_at=? WHERE user_id=? AND workspace_id=?",
            (role, now_iso(), user_id, workspace_id),
        )


def _create_team_workspace(
    store: PlatformStore,
    owner_id: str,
    members: list[tuple[str, str, str]],
    *,
    name: str = "Team Wiki",
) -> dict:
    organization_id = uuid.uuid4().hex
    workspace_id = uuid.uuid4().hex
    created = now_iso()
    with store.connect() as db:
        db.execute("BEGIN IMMEDIATE")
        db.execute(
            "INSERT INTO organizations VALUES(?, 'team', NULL, ?, 'active', ?, ?, ?)",
            (organization_id, name, owner_id, created, created),
        )
        for user_id, organization_role, workspace_role in members:
            db.execute(
                "INSERT INTO organization_members VALUES(?,?,?,'active',?,?,?)",
                (organization_id, user_id, organization_role, owner_id, created, created),
            )
        db.execute(
            """INSERT INTO workspaces(
                id,owner_id,organization_id,root_name,display_name,status,created_at,updated_at
            ) VALUES(?,?,?,?,?,'active',?,?)""",
            (workspace_id, owner_id, organization_id, workspace_id, name, created, created),
        )
        for user_id, _organization_role, workspace_role in members:
            db.execute(
                "INSERT INTO workspace_members VALUES(?,?,?,?,'active',0,?,?,?)",
                (organization_id, workspace_id, user_id, workspace_role, owner_id, created, created),
            )
        db.commit()
    root = store.workspace_root(workspace_id)
    (root / "wiki").mkdir(parents=True, exist_ok=True)
    (root / "raw").mkdir(parents=True, exist_ok=True)
    return {"id": workspace_id, "organization_id": organization_id, "root_name": workspace_id}


def _select_default_workspace(store: PlatformStore, user_id: str, workspace_id: str) -> None:
    with store.connect() as db:
        db.execute("BEGIN IMMEDIATE")
        db.execute("UPDATE workspace_members SET is_default=0 WHERE user_id=?", (user_id,))
        if db.execute(
            "UPDATE workspace_members SET is_default=1 WHERE user_id=? AND workspace_id=? AND status='active'",
            (user_id, workspace_id),
        ).rowcount != 1:
            db.rollback()
            raise FileNotFoundError(workspace_id)
        db.execute(
            "UPDATE sessions SET current_workspace_id=? WHERE user_id=?",
            (workspace_id, user_id),
        )
        db.commit()


def _add_workspace_member(
    store: PlatformStore,
    workspace_id: str,
    user_id: str,
    *,
    organization_role: str = "member",
    workspace_role: str = "editor",
) -> None:
    created = now_iso()
    with store.connect() as db:
        workspace = db.execute(
            "SELECT organization_id,owner_id FROM workspaces WHERE id=?", (workspace_id,),
        ).fetchone()
        db.execute("BEGIN IMMEDIATE")
        db.execute(
            "INSERT INTO organization_members VALUES(?,?,?,'active',?,?,?)",
            (workspace["organization_id"], user_id, organization_role, workspace["owner_id"], created, created),
        )
        db.execute(
            "INSERT INTO workspace_members VALUES(?,?,?,?,'active',0,?,?,?)",
            (workspace["organization_id"], workspace_id, user_id, workspace_role,
             workspace["owner_id"], created, created),
        )
        db.commit()


def test_registration_creates_personal_organization_and_owner_membership(tmp_path: Path):
    store = PlatformStore(tmp_path)
    user, _recovery = store.register("owner@example.com", "Owner", "correct-horse-123")
    _token, context = store.create_session(user["id"])

    assert context.organization_role == "owner"
    assert context.workspace_role == "owner"
    assert context.public()["workspace"]["role"] == "owner"
    with store.connect() as db:
        organization = db.execute(
            "SELECT * FROM organizations WHERE personal_owner_id=?", (user["id"],),
        ).fetchone()
        organization_member = db.execute(
            "SELECT * FROM organization_members WHERE organization_id=? AND user_id=?",
            (organization["id"], user["id"]),
        ).fetchone()
        workspace_member = db.execute(
            "SELECT * FROM workspace_members WHERE workspace_id=? AND user_id=?",
            (user["workspace_id"], user["id"]),
        ).fetchone()
    assert organization["kind"] == "personal"
    assert organization_member["role"] == "owner"
    assert workspace_member["role"] == "owner" and workspace_member["is_default"] == 1


def test_old_workspace_schema_is_backfilled_idempotently(tmp_path: Path):
    state = tmp_path / ".platform"
    state.mkdir(parents=True)
    database = state / "platform.sqlite3"
    created = now_iso()
    with sqlite3.connect(database) as db:
        db.executescript("""
            CREATE TABLE users (
                id TEXT PRIMARY KEY, email TEXT NOT NULL UNIQUE, nickname TEXT NOT NULL,
                password_hash TEXT NOT NULL, role TEXT NOT NULL CHECK(role IN ('user','admin')),
                status TEXT NOT NULL DEFAULT 'active', created_at TEXT NOT NULL
            );
            CREATE TABLE workspaces (
                id TEXT PRIMARY KEY, owner_id TEXT NOT NULL UNIQUE REFERENCES users(id),
                root_name TEXT NOT NULL UNIQUE, display_name TEXT NOT NULL, created_at TEXT NOT NULL
            );
            CREATE TABLE model_settings (
                workspace_id TEXT PRIMARY KEY REFERENCES workspaces(id) ON DELETE CASCADE,
                provider TEXT NOT NULL, base_url_enc TEXT NOT NULL, api_key_enc TEXT NOT NULL,
                model TEXT NOT NULL, updated_at TEXT NOT NULL
            );
        """)
        db.execute(
            "INSERT INTO users VALUES(?,?,?,?,?,?,?)",
            ("u" * 32, "legacy@example.com", "Legacy", "unused", "admin", "active", created),
        )
        db.execute(
            "INSERT INTO workspaces VALUES(?,?,?,?,?)",
            ("w" * 32, "u" * 32, "w" * 32, "Legacy Wiki", created),
        )
        db.execute(
            "INSERT INTO model_settings VALUES(?,?,?,?,?,?)",
            ("w" * 32, "legacy", "encrypted-url", "encrypted-key", "legacy-model", created),
        )

    store = PlatformStore(tmp_path)
    store = PlatformStore(tmp_path)
    with store.connect() as db:
        workspace = db.execute("SELECT * FROM workspaces WHERE id=?", ("w" * 32,)).fetchone()
        assert workspace["organization_id"]
        assert db.execute("SELECT COUNT(*) FROM organizations").fetchone()[0] == 1
        assert db.execute("SELECT COUNT(*) FROM organization_members").fetchone()[0] == 1
        assert db.execute("SELECT COUNT(*) FROM workspace_members").fetchone()[0] == 1
        assert db.execute("SELECT model FROM model_settings WHERE workspace_id=?", ("w" * 32,)).fetchone()[0] == "legacy-model"
        assert db.execute("PRAGMA foreign_key_check").fetchall() == []
        owner_indexes = []
        for index in db.execute("PRAGMA index_list(workspaces)").fetchall():
            if index["unique"]:
                owner_indexes.append([row["name"] for row in db.execute(f"PRAGMA index_info('{index['name']}')")])
        assert ["owner_id"] not in owner_indexes

    team = _create_team_workspace(store, "u" * 32, [("u" * 32, "owner", "owner")])
    assert store.authorize_workspace("u" * 32, team["id"], "workspace.manage")["workspace_role"] == "owner"
    with store.connect() as db:
        assert db.execute("PRAGMA foreign_key_check").fetchall() == []


def test_team_revocation_is_not_recreated_during_restart(tmp_path: Path):
    store = PlatformStore(tmp_path)
    owner, _ = store.register("team-owner@example.com", "Team Owner", "correct-horse-123")
    member, _ = store.register("team-member@example.com", "Team Member", "correct-horse-123")
    team = _create_team_workspace(store, owner["id"], [
        (owner["id"], "owner", "owner"),
        (member["id"], "member", "viewer"),
    ])
    with store.connect() as db:
        db.execute("DELETE FROM workspace_members WHERE workspace_id=? AND user_id=?", (team["id"], owner["id"]))
        db.execute("DELETE FROM organization_members WHERE organization_id=? AND user_id=?", (team["organization_id"], owner["id"]))

    store = PlatformStore(tmp_path)
    store = PlatformStore(tmp_path)
    with pytest.raises(FileNotFoundError):
        store.authorize_workspace(owner["id"], team["id"], "wiki.read")
    assert store.authorize_workspace(member["id"], team["id"], "wiki.read")["workspace_role"] == "viewer"
    with store.connect() as db:
        assert db.execute(
            "SELECT COUNT(*) FROM workspace_members WHERE workspace_id=? AND user_id=?",
            (team["id"], owner["id"]),
        ).fetchone()[0] == 0
        assert db.execute("PRAGMA foreign_key_check").fetchall() == []


def test_workspace_permissions_and_membership_revocation_take_effect_immediately(membership_server):
    app, base = membership_server
    owner = Client(base)
    assert owner.register("roles@example.com", "Roles")[0] == 201
    article_path, _revision = seed_article(app, owner, "Role protected")
    context = context_for(app, owner)

    _set_workspace_role(app.platform, context.user_id, context.workspace_id, "viewer")
    assert owner.request("GET", f"/api/article?path={article_path}")[0] == 200
    assert owner.request("POST", "/api/article/save", {}, key="viewer-write")[0] == 403
    assert owner.request("GET", "/api/settings/model")[0] == 403
    assert owner.request("GET", "/api/account/export")[0] == 403

    _set_workspace_role(app.platform, context.user_id, context.workspace_id, "editor")
    article = owner.request("GET", f"/api/article?path={article_path}")[1]
    status, _saved = owner.request("POST", "/api/article/save", {
        "path": article_path,
        "markdown": article["markdown"] + "\nEditor update.\n",
        "revision": article["revision"],
    }, key="editor-write")
    assert status == 200
    assert owner.request("GET", "/api/settings/model")[0] == 403

    _set_workspace_role(app.platform, context.user_id, context.workspace_id, "owner")
    assert owner.request("GET", "/api/settings/model")[0] == 200
    assert app.platform.authorize_workspace(context.user_id, context.workspace_id, "workspace.manage")["workspace_role"] == "owner"

    with app.platform.connect() as db:
        db.execute(
            "UPDATE workspace_members SET status='suspended',updated_at=? WHERE workspace_id=? AND user_id=?",
            (now_iso(), context.workspace_id, context.user_id),
        )
    assert owner.request("GET", "/api/articles")[0] == 401


def test_platform_admin_has_no_implicit_private_workspace_access(tmp_path: Path):
    store = PlatformStore(tmp_path)
    admin, _ = store.register("admin@example.com", "Admin", "correct-horse-123")
    member, _ = store.register("member@example.com", "Member", "correct-horse-123")
    member_workspace = store.user_workspace(member["id"])

    with pytest.raises(FileNotFoundError):
        store.authorize_workspace(admin["id"], member_workspace["id"], "wiki.read")

    with pytest.raises(PermissionError):
        _set_workspace_role(store, member["id"], member_workspace["id"], "viewer")
        store.authorize_workspace(member["id"], member_workspace["id"], "wiki.write")


def test_submission_actions_follow_workspace_role_and_scope(membership_server):
    app, base = membership_server
    author = Client(base)
    author.register("submission-scope@example.com", "Submission Scope")
    article_path, revision = seed_article(app, author, "Scoped publication")
    context_a = context_for(app, author)

    preview = author.request("POST", "/api/share-previews", {
        "article_path": article_path,
        "source_revision": revision,
        "attribution": "nickname",
    }, key="scope-preview-a")[1]
    approved_submission = author.request(
        "POST", "/api/submissions", {"preview_id": preview["preview_id"]}, key="scope-submit-a",
    )[1]
    app.platform.ai_decide(approved_submission["id"], "pass", {"summary": "accepted"})
    approved = author.request("POST", f"/api/admin/submissions/{approved_submission['id']}/decision", {
        "decision": "approve", "reason": "approved",
    }, key="scope-approve-a")[1]

    article_file = app.platform.workspace_root(context_a.workspace_root_name) / "wiki" / article_path
    article_file.write_text(article_file.read_text(encoding="utf-8") + "\nUpdate for retry test.\n", encoding="utf-8")
    updated_revision = app.workspace_service(context_a).read_article(article_path)["revision"]
    second_preview = author.request("POST", "/api/share-previews", {
        "article_path": article_path,
        "source_revision": updated_revision,
        "attribution": "nickname",
    }, key="scope-preview-failed")[1]
    failed_submission = author.request(
        "POST", "/api/submissions", {"preview_id": second_preview["preview_id"]}, key="scope-submit-failed",
    )[1]
    with app.platform.connect() as db:
        db.execute("UPDATE submissions SET status='ai_failed' WHERE id=?", (failed_submission["id"],))

    _set_workspace_role(app.platform, context_a.user_id, context_a.workspace_id, "viewer")
    assert author.request(
        "POST", f"/api/submissions/{failed_submission['id']}/ai-retry", {}, key="viewer-retry",
    )[0] == 403
    assert author.request(
        "POST", f"/api/submissions/{approved_submission['id']}/withdraw", {}, key="viewer-withdraw",
    )[0] == 403
    assert Client(base).request("GET", f"/api/public/entries/{approved['public_entry_id']}")[0] == 200

    _set_workspace_role(app.platform, context_a.user_id, context_a.workspace_id, "editor")
    assert author.request(
        "POST", f"/api/submissions/{failed_submission['id']}/ai-retry", {}, key="editor-retry",
    )[0] == 200
    assert author.request(
        "POST", f"/api/submissions/{approved_submission['id']}/withdraw", {}, key="editor-withdraw",
    )[0] == 200
    assert Client(base).request("GET", f"/api/public/entries/{approved['public_entry_id']}")[0] == 404


def test_submissions_and_publication_state_do_not_cross_workspaces(membership_server):
    app, base = membership_server
    author = Client(base)
    author.register("two-spaces@example.com", "Two Spaces")
    path_a, revision_a = seed_article(app, author, "Same title")
    context_a = context_for(app, author)
    preview_a = author.request("POST", "/api/share-previews", {
        "article_path": path_a, "source_revision": revision_a, "attribution": "nickname",
    }, key="two-preview-a")[1]
    submission_a = author.request("POST", "/api/submissions", {"preview_id": preview_a["preview_id"]}, key="two-submit-a")[1]
    app.platform.ai_decide(submission_a["id"], "pass", {"summary": "accepted A"})
    public_a = author.request("POST", f"/api/admin/submissions/{submission_a['id']}/decision", {
        "decision": "approve", "reason": "approved A",
    }, key="two-approve-a")[1]

    team = _create_team_workspace(app.platform, context_a.user_id, [(context_a.user_id, "owner", "owner")], name="Second Wiki")
    _select_default_workspace(app.platform, context_a.user_id, team["id"])
    assert author.request("GET", "/api/submissions")[1] == []
    assert author.request("GET", f"/api/submissions/{submission_a['id']}")[0] == 404
    assert author.request(
        "POST", f"/api/submissions/{submission_a['id']}/withdraw", {}, key="cross-space-withdraw",
    )[0] == 404

    path_b, revision_b = seed_article(app, author, "Same title")
    article_b = author.request("GET", f"/api/article?path={path_b}")[1]
    assert article_b["publication"]["state"] == "not_published"
    preview_b = author.request("POST", "/api/share-previews", {
        "article_path": path_b, "source_revision": revision_b, "attribution": "nickname",
    }, key="two-preview-b")[1]
    submission_b = author.request("POST", "/api/submissions", {"preview_id": preview_b["preview_id"]}, key="two-submit-b")[1]
    app.platform.ai_decide(submission_b["id"], "pass", {"summary": "accepted B"})
    public_b = author.request("POST", f"/api/admin/submissions/{submission_b['id']}/decision", {
        "decision": "approve", "reason": "approved B",
    }, key="two-approve-b")[1]
    assert public_b["public_entry_id"] != public_a["public_entry_id"]
    assert Client(base).request("GET", f"/api/public/entries/{public_b['public_entry_id']}")[1]["version"] == 1

    _select_default_workspace(app.platform, context_a.user_id, context_a.workspace_id)
    article_a = author.request("GET", f"/api/article?path={path_a}")[1]
    assert article_a["publication"]["public_entry_id"] == public_a["public_entry_id"]


def test_account_deletion_preserves_shared_team_workspace(membership_server):
    app, base = membership_server
    owner, member, viewer = Client(base), Client(base), Client(base)
    owner.register("delete-owner@example.com", "Delete Owner")
    member.register("delete-member@example.com", "Delete Member")
    viewer.register("delete-viewer@example.com", "Delete Viewer")
    owner_context = context_for(app, owner)
    member_context = context_for(app, member)
    viewer_context = context_for(app, viewer)
    personal_root = app.platform.workspace_root(owner_context.workspace_root_name)
    team = _create_team_workspace(app.platform, owner_context.user_id, [
        (owner_context.user_id, "owner", "owner"),
        (member_context.user_id, "member", "editor"),
        (viewer_context.user_id, "member", "viewer"),
    ], name="Persistent Team")
    _select_default_workspace(app.platform, owner_context.user_id, team["id"])
    team_path, _ = seed_article(app, owner, "Team survives")
    team_root = app.platform.workspace_root(team["root_name"])
    app.platform.save_model(team["id"], "openai-compatible", "https://models.example.test/v1", "team-key", "team-model")

    assert owner.request(
        "POST", "/api/account/delete", {"password": "wrong-password-123"}, key="reject-delete-shared-owner",
    )[0] == 422
    assert owner.request("GET", f"/api/article?path={team_path}")[0] == 200
    assert team_root.exists()

    status, payload = owner.request(
        "POST", "/api/account/delete", {"password": "correct-horse-123"}, key="delete-shared-owner",
    )
    assert status == 200 and payload["deleted"] is True
    assert not personal_root.exists()
    assert team_root.exists()
    assert (team_root / "wiki" / team_path).exists()
    assert app.platform.load_model(team["id"])["model"] == "team-model"
    with app.platform.connect() as db:
        assert db.execute("SELECT COUNT(*) FROM workspace_members WHERE user_id=?", (owner_context.user_id,)).fetchone()[0] == 0
        promoted = db.execute("SELECT role FROM workspace_members WHERE workspace_id=? AND user_id=?", (
            team["id"], member_context.user_id,
        )).fetchone()
        assert promoted["role"] == "owner"
        organization_owner = db.execute(
            "SELECT user_id FROM organization_members WHERE organization_id=? AND role='owner' AND status='active'",
            (team["organization_id"],),
        ).fetchone()
        assert organization_owner is not None
        assert db.execute("SELECT owner_id FROM workspaces WHERE id=?", (team["id"],)).fetchone()[0] == member_context.user_id
        assert db.execute(
            "SELECT COUNT(*) FROM audit_events WHERE action='workspace.owner_transfer' AND object_id=?",
            (team["id"],),
        ).fetchone()[0] == 1
        transfer = db.execute(
            "SELECT detail_json FROM audit_events WHERE action='workspace.owner_transfer' AND object_id=?",
            (team["id"],),
        ).fetchone()
        assert json.loads(transfer["detail_json"]) == {
            "old_owner_id": owner_context.user_id,
            "new_owner_id": member_context.user_id,
            "previous_role": "editor",
            "reason": "account_deletion",
        }

    _select_default_workspace(app.platform, member_context.user_id, team["id"])
    assert member.request("GET", f"/api/article?path={team_path}")[0] == 200
    promoted_context = context_for(app, member)
    assert app.platform.authorize_workspace(
        promoted_context.user_id, promoted_context.workspace_id, "workspace.manage",
    )["workspace_role"] == "owner"
    assert app.platform.authorize_workspace(
        promoted_context.user_id, promoted_context.workspace_id, "model.manage",
    )["workspace_role"] == "owner"
    assert app.export_workspace(promoted_context)
    assert owner.request("GET", "/api/articles")[0] == 401


def test_shared_personal_workspace_blocks_account_deletion(membership_server):
    app, base = membership_server
    owner, member = Client(base), Client(base)
    owner.register("personal-owner@example.com", "Personal Owner")
    member.register("personal-member@example.com", "Personal Member")
    owner_context = context_for(app, owner)
    member_context = context_for(app, member)
    _add_workspace_member(app.platform, owner_context.workspace_id, member_context.user_id, workspace_role="editor")
    article_path, _ = seed_article(app, owner, "Shared personal article")
    personal_root = app.platform.workspace_root(owner_context.workspace_root_name)
    app.platform.save_model(
        owner_context.workspace_id, "openai-compatible", "https://models.example.test/v1",
        "personal-key", "personal-model",
    )
    article_bytes = (personal_root / "wiki" / article_path).read_bytes()
    before_model = app.platform.load_model(owner_context.workspace_id)
    with app.platform.connect() as db:
        personal_workspace = db.execute(
            "SELECT owner_id,organization_id FROM workspaces WHERE id=?", (owner_context.workspace_id,),
        ).fetchone()
        personal_organization_id = personal_workspace["organization_id"]
        before = {
            "user": tuple(db.execute(
                "SELECT email,nickname,password_hash,status FROM users WHERE id=?", (owner_context.user_id,),
            ).fetchone()),
            "organization": tuple(db.execute(
                "SELECT kind,personal_owner_id,status FROM organizations WHERE id=?", (personal_organization_id,),
            ).fetchone()),
            "workspace_owner": personal_workspace["owner_id"],
            "sessions": [tuple(row) for row in db.execute(
                "SELECT token_hash,user_id,csrf_hash,expires_at,created_at FROM sessions "
                "WHERE user_id=? ORDER BY token_hash", (owner_context.user_id,),
            )],
            "recovery": [tuple(row) for row in db.execute(
                "SELECT code_hash,user_id,expires_at,used_at FROM recovery_codes "
                "WHERE user_id=? ORDER BY code_hash", (owner_context.user_id,),
            )],
            "organization_members": [tuple(row) for row in db.execute(
                "SELECT user_id,role,status,added_by,created_at,updated_at FROM organization_members "
                "WHERE organization_id=? ORDER BY user_id", (personal_organization_id,),
            )],
            "workspace_members": [tuple(row) for row in db.execute(
                "SELECT user_id,role,status,is_default,added_by,created_at,updated_at FROM workspace_members "
                "WHERE workspace_id=? ORDER BY user_id", (owner_context.workspace_id,),
            )],
            "audit_count": db.execute(
                "SELECT COUNT(*) FROM audit_events WHERE actor_id=?", (owner_context.user_id,),
            ).fetchone()[0],
        }

    status, payload = owner.request(
        "POST", "/api/account/delete", {"password": "correct-horse-123"}, key="reject-shared-personal",
    )
    assert status == 422
    assert "personal workspaces" in payload["error"]
    assert personal_root.exists()
    assert (personal_root / "wiki" / article_path).read_bytes() == article_bytes
    assert app.platform.load_model(owner_context.workspace_id) == before_model
    assert owner.request("GET", f"/api/article?path={article_path}")[0] == 200
    with app.platform.connect() as db:
        after = {
            "user": tuple(db.execute(
                "SELECT email,nickname,password_hash,status FROM users WHERE id=?", (owner_context.user_id,),
            ).fetchone()),
            "organization": tuple(db.execute(
                "SELECT kind,personal_owner_id,status FROM organizations WHERE id=?", (personal_organization_id,),
            ).fetchone()),
            "workspace_owner": db.execute(
                "SELECT owner_id FROM workspaces WHERE id=?", (owner_context.workspace_id,),
            ).fetchone()[0],
            "sessions": [tuple(row) for row in db.execute(
                "SELECT token_hash,user_id,csrf_hash,expires_at,created_at FROM sessions "
                "WHERE user_id=? ORDER BY token_hash", (owner_context.user_id,),
            )],
            "recovery": [tuple(row) for row in db.execute(
                "SELECT code_hash,user_id,expires_at,used_at FROM recovery_codes "
                "WHERE user_id=? ORDER BY code_hash", (owner_context.user_id,),
            )],
            "organization_members": [tuple(row) for row in db.execute(
                "SELECT user_id,role,status,added_by,created_at,updated_at FROM organization_members "
                "WHERE organization_id=? ORDER BY user_id", (personal_organization_id,),
            )],
            "workspace_members": [tuple(row) for row in db.execute(
                "SELECT user_id,role,status,is_default,added_by,created_at,updated_at FROM workspace_members "
                "WHERE workspace_id=? ORDER BY user_id", (owner_context.workspace_id,),
            )],
            "audit_count": db.execute(
                "SELECT COUNT(*) FROM audit_events WHERE actor_id=?", (owner_context.user_id,),
            ).fetchone()[0],
        }
        assert after == before
    _select_default_workspace(app.platform, member_context.user_id, owner_context.workspace_id)
    assert member.request("GET", f"/api/article?path={article_path}")[0] == 200


def test_unshared_personal_workspace_deletion_still_cleans_root(membership_server):
    app, base = membership_server
    owner = Client(base)
    owner.register("unshared-owner@example.com", "Unshared Owner")
    owner_context = context_for(app, owner)
    personal_root = app.platform.workspace_root(owner_context.workspace_root_name)
    article_path, _ = seed_article(app, owner, "Private article")
    assert (personal_root / "wiki" / article_path).exists()

    status, payload = owner.request(
        "POST", "/api/account/delete", {"password": "correct-horse-123"}, key="delete-unshared-personal",
    )
    assert status == 200 and payload["deleted"] is True
    assert not personal_root.exists()
    with app.platform.connect() as db:
        assert db.execute("SELECT status FROM users WHERE id=?", (owner_context.user_id,)).fetchone()[0] == "deleted"
        assert db.execute("SELECT COUNT(*) FROM workspace_members WHERE user_id=?", (owner_context.user_id,)).fetchone()[0] == 0
        assert db.execute(
            "SELECT COUNT(*) FROM audit_events WHERE actor_id=? AND action='user.delete'",
            (owner_context.user_id,),
        ).fetchone()[0] == 1


def test_shared_personal_workspace_rolls_back_team_transfer(membership_server):
    app, base = membership_server
    owner, member = Client(base), Client(base)
    owner.register("mixed-owner@example.com", "Mixed Owner")
    member.register("mixed-member@example.com", "Mixed Member")
    owner_context = context_for(app, owner)
    member_context = context_for(app, member)
    _add_workspace_member(
        app.platform, owner_context.workspace_id, member_context.user_id, workspace_role="viewer",
    )
    team = _create_team_workspace(app.platform, owner_context.user_id, [
        (owner_context.user_id, "owner", "owner"),
        (member_context.user_id, "member", "editor"),
    ], name="Transferable But Blocked")
    _select_default_workspace(app.platform, owner_context.user_id, team["id"])
    team_path, _ = seed_article(app, owner, "Team remains unchanged")
    team_root = app.platform.workspace_root(team["root_name"])
    app.platform.save_model(
        team["id"], "openai-compatible", "https://models.example.test/v1", "team-key", "mixed-model",
    )

    status, _payload = owner.request(
        "POST", "/api/account/delete", {"password": "correct-horse-123"}, key="shared-personal-blocks-team",
    )
    assert status == 422
    assert (team_root / "wiki" / team_path).exists()
    assert app.platform.load_model(team["id"])["model"] == "mixed-model"
    assert owner.request("GET", f"/api/article?path={team_path}")[0] == 200
    with app.platform.connect() as db:
        assert db.execute("SELECT status FROM users WHERE id=?", (owner_context.user_id,)).fetchone()[0] == "active"
        assert db.execute("SELECT owner_id FROM workspaces WHERE id=?", (team["id"],)).fetchone()[0] == owner_context.user_id
        assert db.execute(
            "SELECT role FROM workspace_members WHERE workspace_id=? AND user_id=?",
            (team["id"], member_context.user_id),
        ).fetchone()[0] == "editor"
        assert db.execute(
            "SELECT COUNT(*) FROM audit_events WHERE actor_id=? AND action IN "
            "('user.delete','workspace.owner_transfer','organization.owner_transfer')",
            (owner_context.user_id,),
        ).fetchone()[0] == 0


def test_account_deletion_preserves_existing_second_team_owner(membership_server):
    app, base = membership_server
    departing, successor = Client(base), Client(base)
    departing.register("departing-owner@example.com", "Departing Owner")
    successor.register("existing-owner@example.com", "Existing Owner")
    departing_context = context_for(app, departing)
    successor_context = context_for(app, successor)
    team = _create_team_workspace(app.platform, departing_context.user_id, [
        (departing_context.user_id, "owner", "owner"),
        (successor_context.user_id, "owner", "owner"),
    ], name="Two Owners")
    _select_default_workspace(app.platform, departing_context.user_id, team["id"])
    team_path, _ = seed_article(app, departing, "Second owner survives")
    app.platform.save_model(
        team["id"], "openai-compatible", "https://models.example.test/v1", "team-key", "owner-model",
    )

    assert departing.request(
        "POST", "/api/account/delete", {"password": "correct-horse-123"}, key="delete-with-successor",
    )[0] == 200
    with app.platform.connect() as db:
        assert db.execute("SELECT owner_id FROM workspaces WHERE id=?", (team["id"],)).fetchone()[0] == successor_context.user_id
        assert db.execute(
            "SELECT role FROM workspace_members WHERE workspace_id=? AND user_id=?",
            (team["id"], successor_context.user_id),
        ).fetchone()[0] == "owner"
        assert db.execute(
            "SELECT role FROM organization_members WHERE organization_id=? AND user_id=?",
            (team["organization_id"], successor_context.user_id),
        ).fetchone()[0] == "owner"

    _select_default_workspace(app.platform, successor_context.user_id, team["id"])
    assert successor.request("GET", f"/api/article?path={team_path}")[0] == 200
    successor_team_context = context_for(app, successor)
    assert app.platform.authorize_workspace(
        successor_team_context.user_id, successor_team_context.workspace_id, "workspace.manage",
    )["workspace_role"] == "owner"
    assert app.platform.authorize_workspace(
        successor_team_context.user_id, successor_team_context.workspace_id, "model.manage",
    )["workspace_role"] == "owner"
    assert app.platform.load_model(team["id"])["model"] == "owner-model"
    assert app.export_workspace(successor_team_context)


def test_account_deletion_rolls_back_when_any_team_has_no_successor(membership_server):
    app, base = membership_server
    owner, member = Client(base), Client(base)
    owner.register("atomic-owner@example.com", "Atomic Owner")
    member.register("atomic-member@example.com", "Atomic Member")
    owner_context = context_for(app, owner)
    member_context = context_for(app, member)
    transferable = _create_team_workspace(app.platform, owner_context.user_id, [
        (owner_context.user_id, "owner", "owner"),
        (member_context.user_id, "member", "editor"),
    ], name="Transferable")
    blocked = _create_team_workspace(app.platform, owner_context.user_id, [
        (owner_context.user_id, "owner", "owner"),
    ], name="No Successor")
    _select_default_workspace(app.platform, owner_context.user_id, transferable["id"])
    personal_root = app.platform.workspace_root(owner_context.workspace_root_name)
    team_path, _ = seed_article(app, owner, "Atomic team article")
    team_root = app.platform.workspace_root(transferable["root_name"])
    blocked_root = app.platform.workspace_root(blocked["root_name"])
    (blocked_root / "wiki" / "blocked.md").write_text("# Blocked\n", encoding="utf-8")
    app.platform.save_model(
        transferable["id"], "openai-compatible", "https://models.example.test/v1", "team-key", "atomic-model",
    )
    with app.platform.connect() as db:
        before = {
            "sessions": [tuple(row) for row in db.execute(
                "SELECT token_hash,user_id,csrf_hash,expires_at,created_at FROM sessions WHERE user_id=? ORDER BY token_hash",
                (owner_context.user_id,),
            )],
            "recovery": [tuple(row) for row in db.execute(
                "SELECT code_hash,user_id,expires_at,used_at FROM recovery_codes WHERE user_id=? ORDER BY code_hash",
                (owner_context.user_id,),
            )],
            "workspace_members": [tuple(row) for row in db.execute(
                "SELECT organization_id,workspace_id,user_id,role,status,is_default,added_by,created_at,updated_at "
                "FROM workspace_members WHERE user_id=? OR workspace_id IN (?,?) ORDER BY workspace_id,user_id",
                (owner_context.user_id, transferable["id"], blocked["id"]),
            )],
            "organization_members": [tuple(row) for row in db.execute(
                "SELECT organization_id,user_id,role,status,added_by,created_at,updated_at "
                "FROM organization_members WHERE user_id=? OR organization_id IN (?,?) ORDER BY organization_id,user_id",
                (owner_context.user_id, transferable["organization_id"], blocked["organization_id"]),
            )],
        }

    status, _payload = owner.request(
        "POST", "/api/account/delete", {"password": "correct-horse-123"}, key="atomic-delete",
    )
    assert status == 422
    assert personal_root.exists()
    assert (team_root / "wiki" / team_path).exists()
    assert (blocked_root / "wiki" / "blocked.md").read_text(encoding="utf-8") == "# Blocked\n"
    assert app.platform.load_model(transferable["id"])["model"] == "atomic-model"
    assert owner.request("GET", "/api/articles")[0] == 200
    with app.platform.connect() as db:
        assert db.execute("SELECT status FROM users WHERE id=?", (owner_context.user_id,)).fetchone()[0] == "active"
        assert db.execute("SELECT owner_id FROM workspaces WHERE id=?", (transferable["id"],)).fetchone()[0] == owner_context.user_id
        assert db.execute("SELECT owner_id FROM workspaces WHERE id=?", (blocked["id"],)).fetchone()[0] == owner_context.user_id
        assert db.execute(
            "SELECT role FROM workspace_members WHERE workspace_id=? AND user_id=?",
            (transferable["id"], member_context.user_id),
        ).fetchone()[0] == "editor"
        assert db.execute(
            "SELECT COUNT(*) FROM audit_events WHERE action IN ('workspace.owner_transfer','organization.owner_transfer') AND actor_id=?",
            (owner_context.user_id,),
        ).fetchone()[0] == 0
        after = {
            "sessions": [tuple(row) for row in db.execute(
                "SELECT token_hash,user_id,csrf_hash,expires_at,created_at FROM sessions WHERE user_id=? ORDER BY token_hash",
                (owner_context.user_id,),
            )],
            "recovery": [tuple(row) for row in db.execute(
                "SELECT code_hash,user_id,expires_at,used_at FROM recovery_codes WHERE user_id=? ORDER BY code_hash",
                (owner_context.user_id,),
            )],
            "workspace_members": [tuple(row) for row in db.execute(
                "SELECT organization_id,workspace_id,user_id,role,status,is_default,added_by,created_at,updated_at "
                "FROM workspace_members WHERE user_id=? OR workspace_id IN (?,?) ORDER BY workspace_id,user_id",
                (owner_context.user_id, transferable["id"], blocked["id"]),
            )],
            "organization_members": [tuple(row) for row in db.execute(
                "SELECT organization_id,user_id,role,status,added_by,created_at,updated_at "
                "FROM organization_members WHERE user_id=? OR organization_id IN (?,?) ORDER BY organization_id,user_id",
                (owner_context.user_id, transferable["organization_id"], blocked["organization_id"]),
            )],
        }
        assert after == before
