from __future__ import annotations

import sqlite3
import threading
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
        """)
        db.execute(
            "INSERT INTO users VALUES(?,?,?,?,?,?,?)",
            ("u" * 32, "legacy@example.com", "Legacy", "unused", "admin", "active", created),
        )
        db.execute(
            "INSERT INTO workspaces VALUES(?,?,?,?,?)",
            ("w" * 32, "u" * 32, "w" * 32, "Legacy Wiki", created),
        )

    store = PlatformStore(tmp_path)
    store = PlatformStore(tmp_path)
    with store.connect() as db:
        workspace = db.execute("SELECT * FROM workspaces WHERE id=?", ("w" * 32,)).fetchone()
        assert workspace["organization_id"]
        assert db.execute("SELECT COUNT(*) FROM organizations").fetchone()[0] == 1
        assert db.execute("SELECT COUNT(*) FROM organization_members").fetchone()[0] == 1
        assert db.execute("SELECT COUNT(*) FROM workspace_members").fetchone()[0] == 1
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
