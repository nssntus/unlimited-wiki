from __future__ import annotations

import http.client
import json
import threading
import time
from http.server import BaseHTTPRequestHandler
from pathlib import Path

import pytest

import serve as serve_module
from deployment import DeploymentConfig
from platform_store import PlatformStore, hash_token
from serve import BoundedThreadingHTTPServer, create_app, create_server


def request(server, method: str, path: str, *, body: dict | None = None, headers: dict | None = None):
    raw = json.dumps(body).encode("utf-8") if body is not None else None
    values = {"Accept": "application/json", **(headers or {})}
    if raw is not None:
        values.setdefault("Content-Type", "application/json")
        values.setdefault("Origin", "https://wiki.intra.test")
        values.setdefault("Idempotency-Key", "deployment-test")
    connection = http.client.HTTPConnection(*server.server_address, timeout=5)
    connection.request(method, path, body=raw, headers=values)
    response = connection.getresponse()
    payload = json.loads(response.read().decode("utf-8"))
    result = response.status, dict(response.headers), payload
    connection.close()
    return result


@pytest.fixture
def lan_server(tmp_path: Path):
    (tmp_path / "viewer" / "dist").mkdir(parents=True)
    (tmp_path / "viewer" / "dist" / "index.html").write_text("<!doctype html>", encoding="utf-8")
    deployment = DeploymentConfig(
        public_origin="https://wiki.intra.test",
        trusted_proxy_cidrs=("127.0.0.1/32",),
        registration_mode="bootstrap",
        min_free_bytes=0,
    )
    app = create_app(
        tmp_path, tmp_path / "viewer", start_worker=False, multi_user=True,
        deployment_config=deployment,
    )
    server = create_server(app, port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield app, server
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
        app.close()


def proxy_headers(**extra):
    return {
        "Host": "wiki.intra.test",
        "X-Forwarded-Proto": "https",
        "X-Forwarded-For": "10.23.4.5",
        **extra,
    }


def test_lan_configuration_is_fail_closed():
    with pytest.raises(ValueError, match="HTTPS origin"):
        DeploymentConfig(public_origin="http://wiki.intra.test", trusted_proxy_cidrs=("127.0.0.1/32",))
    with pytest.raises(ValueError, match="TRUSTED_PROXY"):
        DeploymentConfig(public_origin="https://wiki.intra.test", registration_mode="closed")
    with pytest.raises(ValueError, match="bootstrap, invite, or closed"):
        DeploymentConfig(
            public_origin="https://wiki.intra.test",
            trusted_proxy_cidrs=("127.0.0.1/32",),
            registration_mode="open",
        )


def test_proxy_chain_uses_first_untrusted_hop_from_the_right():
    config = DeploymentConfig()
    assert config.client_ip("127.0.0.1", "203.0.113.7") == "127.0.0.1"
    trusted = DeploymentConfig(trusted_proxy_cidrs=("127.0.0.0/8", "10.0.0.0/8"))
    assert trusted.client_ip("127.0.0.1", "198.51.100.9, 203.0.113.8, 10.2.0.4") == "203.0.113.8"
    with pytest.raises(ValueError):
        trusted.client_ip("127.0.0.1", "not-an-ip")


def test_lan_host_origin_cookie_and_bootstrap_registration(lan_server):
    app, server = lan_server
    headers = proxy_headers()
    status, response_headers, config = request(server, "GET", "/api/platform/config", headers=headers)
    assert status == 200 and config["registration_enabled"] is True
    assert response_headers["Strict-Transport-Security"] == "max-age=31536000"

    status, response_headers, registered = request(server, "POST", "/api/auth/register", headers=headers, body={
        "email": "owner@example.com", "nickname": "Owner", "password": "correct-horse-123",
    })
    assert status == 201 and registered["user"]["role"] == "admin"
    assert registered["migration"] == {"status": "not_needed"}
    assert registered["recovery_code"]
    cookie = response_headers["Set-Cookie"]
    assert cookie.startswith("__Host-wiki_session=")
    assert "; Secure" in cookie and "; HttpOnly" in cookie and "; SameSite=Strict" in cookie
    assert "Domain=" not in cookie
    assert app.platform.user_count() == 1

    assert request(server, "GET", "/api/platform/config", headers=headers)[2]["registration_enabled"] is False
    status, _response_headers, closed = request(server, "POST", "/api/auth/register", headers=headers, body={
        "email": "second@example.com", "nickname": "Second", "password": "correct-horse-123",
    })
    assert status == 409
    assert closed == {"error": "registration is closed", "code": "registration_closed"}
    assert app.platform.user_count() == 1
    with app.platform.connect() as db:
        assert db.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] == 1
        assert db.execute("SELECT COUNT(*) FROM audit_events WHERE action='user.register'").fetchone()[0] == 1


def test_bootstrap_registration_succeeds_with_read_only_project_root(lan_server):
    app, server = lan_server
    root = app.project_root
    original_mode = root.stat().st_mode & 0o777
    root.chmod(0o555)
    try:
        status, response_headers, registered = request(
            server, "POST", "/api/auth/register", headers=proxy_headers(), body={
                "email": "readonly@example.com", "nickname": "Read Only", "password": "correct-horse-123",
            },
        )
    finally:
        root.chmod(original_mode)
    assert status == 201
    assert registered["migration"] == {"status": "not_needed"}
    assert registered["recovery_code"]
    assert "Set-Cookie" in response_headers
    assert not (root / ".wiki-state").exists()
    assert not (root / ".runtime").exists()


def test_concurrent_bootstrap_http_has_one_clear_winner(lan_server):
    app, server = lan_server
    barrier = threading.Barrier(3)
    results: list[tuple[int, dict]] = []

    def register(index: int):
        barrier.wait()
        status, _headers, payload = request(
            server, "POST", "/api/auth/register", headers=proxy_headers(), body={
                "email": f"owner{index}@example.com", "nickname": f"Owner {index}",
                "password": "correct-horse-123",
            },
        )
        results.append((status, payload))

    threads = [threading.Thread(target=register, args=(index,)) for index in range(2)]
    for thread in threads:
        thread.start()
    barrier.wait()
    for thread in threads:
        thread.join(timeout=5)
    assert sorted(status for status, _payload in results) == [201, 409]
    loser = next(payload for status, payload in results if status == 409)
    assert loser == {"error": "registration is closed", "code": "registration_closed"}
    assert app.platform.user_count() == 1
    with app.platform.connect() as db:
        assert db.execute("SELECT COUNT(*) FROM recovery_codes WHERE used_at IS NULL").fetchone()[0] == 1
        assert db.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] == 1


def test_bootstrap_migration_failure_is_reported_without_failing_registration(lan_server, monkeypatch):
    app, server = lan_server
    monkeypatch.setattr(
        serve_module, "migrate_legacy_workspace",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("injected migration failure")),
    )
    status, response_headers, registered = request(
        server, "POST", "/api/auth/register", headers=proxy_headers(), body={
            "email": "migration@example.com", "nickname": "Migration", "password": "correct-horse-123",
        },
    )
    assert status == 201
    assert registered["migration"] == {"status": "retry_required"}
    assert registered["recovery_code"]
    assert "Set-Cookie" in response_headers
    with app.platform.connect() as db:
        assert db.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 1
        assert db.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] == 1
        audit = db.execute(
            "SELECT detail_json FROM audit_events WHERE action='workspace.migrate_legacy_failed'"
        ).fetchone()
    assert json.loads(audit["detail_json"]) == {"code": "legacy_migration_failed"}


def test_authenticated_user_rotates_recovery_code_with_password(lan_server):
    app, server = lan_server
    status, response_headers, registered = request(
        server, "POST", "/api/auth/register", headers=proxy_headers(), body={
            "email": "recovery@example.com", "nickname": "Recovery", "password": "correct-horse-123",
        },
    )
    assert status == 201
    old_code = registered["recovery_code"]
    authenticated_headers = proxy_headers(**{
        "Cookie": response_headers["Set-Cookie"].split(";", 1)[0],
        "X-CSRF-Token": registered["csrf_token"],
    })
    assert request(
        server, "POST", "/api/account/recovery-code", headers=proxy_headers(),
        body={"password": "correct-horse-123"},
    )[0] == 401
    assert request(
        server, "POST", "/api/account/recovery-code",
        headers=proxy_headers(Cookie=authenticated_headers["Cookie"]),
        body={"password": "correct-horse-123"},
    )[0] == 403
    with app.platform.connect() as db:
        db.execute(
            "UPDATE sessions SET current_workspace_id=NULL WHERE user_id=?",
            (registered["user"]["id"],),
        )

    assert request(
        server, "POST", "/api/account/recovery-code", headers=authenticated_headers,
        body={"password": "wrong-password"},
    )[0] == 422
    with app.platform.connect() as db:
        assert db.execute(
            "SELECT COUNT(*) FROM recovery_codes WHERE used_at IS NULL"
        ).fetchone()[0] == 1

    status, _headers, rotated = request(
        server, "POST", "/api/account/recovery-code", headers=authenticated_headers,
        body={"password": "correct-horse-123"},
    )
    assert status == 200
    assert rotated["expires_in_hours"] == 24
    assert rotated["recovery_code"] != old_code
    with app.platform.connect() as db:
        active = db.execute(
            "SELECT code_hash FROM recovery_codes WHERE used_at IS NULL"
        ).fetchall()
        assert [row["code_hash"] for row in active] == [hash_token(rotated["recovery_code"])]
        assert db.execute(
            "SELECT COUNT(*) FROM audit_events WHERE action='user.recovery_code.rotate'"
        ).fetchone()[0] == 1
    assert old_code.encode() not in app.platform.db_path.read_bytes()
    assert rotated["recovery_code"].encode() not in app.platform.db_path.read_bytes()

    assert request(server, "POST", "/api/auth/recover", headers=proxy_headers(), body={
        "email": "recovery@example.com", "recovery_code": old_code,
        "new_password": "new-password-123",
    })[0] == 400
    assert request(server, "POST", "/api/auth/recover", headers=proxy_headers(), body={
        "email": "recovery@example.com", "recovery_code": rotated["recovery_code"],
        "new_password": "new-password-123",
    })[0] == 200


def test_lan_rejects_wrong_host_origin_and_proxy_scheme(lan_server):
    _app, server = lan_server
    assert request(server, "GET", "/api/platform/config", headers=proxy_headers(Host="wiki.intra.test.evil"))[0] == 403
    assert request(server, "GET", "/api/platform/config", headers=proxy_headers(**{"X-Forwarded-Proto": "http"}))[0] == 400
    assert request(server, "POST", "/api/auth/login", headers=proxy_headers(Origin="https://evil.intra.test"), body={
        "email": "nobody@example.com", "password": "wrong-password-123",
    })[0] == 403


def test_health_and_readiness_do_not_require_session_or_proxy_headers(lan_server):
    app, server = lan_server
    assert request(server, "GET", "/healthz")[2] == {"status": "ok"}
    status, _headers, payload = request(server, "GET", "/readyz")
    assert status == 200 and payload["status"] == "ready"
    assert app._services == {}


def test_rate_limit_is_atomic_and_persists_across_store_instances(tmp_path: Path):
    store = PlatformStore(tmp_path)
    results: list[int] = []
    barrier = threading.Barrier(6)

    def consume():
        barrier.wait()
        results.append(store.consume_rate_limit("login:ip:192.0.2.4", limit=3, window_seconds=60, now=100))

    threads = [threading.Thread(target=consume) for _ in range(5)]
    for thread in threads:
        thread.start()
    barrier.wait()
    for thread in threads:
        thread.join(timeout=2)
    assert results.count(0) == 3
    assert results.count(60) == 2
    restarted = PlatformStore(tmp_path)
    assert restarted.consume_rate_limit("login:ip:192.0.2.4", limit=3, window_seconds=60, now=101) == 59
    assert restarted.consume_rate_limit("login:ip:192.0.2.4", limit=3, window_seconds=60, now=160) == 0


def test_bootstrap_registration_allows_exactly_one_user(tmp_path: Path):
    store = PlatformStore(tmp_path)
    barrier = threading.Barrier(3)
    results: list[str] = []

    def register(index: int):
        barrier.wait()
        try:
            store.register(
                f"owner{index}@example.com", f"Owner {index}", "correct-horse-123",
                first_user_only=True,
            )
            results.append("created")
        except PermissionError:
            results.append("closed")

    threads = [threading.Thread(target=register, args=(index,)) for index in range(2)]
    for thread in threads:
        thread.start()
    barrier.wait()
    for thread in threads:
        thread.join(timeout=3)
    assert sorted(results) == ["closed", "created"]
    assert store.user_count() == 1


def test_register_rolls_back_database_and_workspace_when_audit_fails(tmp_path: Path, monkeypatch):
    store = PlatformStore(tmp_path)

    def fail_audit(*_args, **_kwargs):
        raise OSError("injected audit failure")

    monkeypatch.setattr(store, "_audit", fail_audit)
    with pytest.raises(OSError, match="injected audit failure"):
        store.register("owner@example.com", "Owner", "correct-horse-123", initial_session=True)
    assert store.user_count() == 0
    assert list(store.spaces_root.iterdir()) == []
    with store.connect() as db:
        assert db.execute("SELECT COUNT(*) FROM recovery_codes").fetchone()[0] == 0
        assert db.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] == 0
        assert db.execute("SELECT COUNT(*) FROM audit_events").fetchone()[0] == 0


def test_concurrent_recovery_code_rotation_leaves_one_active_code(tmp_path: Path):
    store = PlatformStore(tmp_path)
    user, _old_code = store.register("owner@example.com", "Owner", "correct-horse-123")
    barrier = threading.Barrier(3)
    issued: list[str] = []

    def rotate():
        barrier.wait()
        issued.append(store.rotate_recovery_code(user["id"], "correct-horse-123"))

    threads = [threading.Thread(target=rotate) for _ in range(2)]
    for thread in threads:
        thread.start()
    barrier.wait()
    for thread in threads:
        thread.join(timeout=3)
    assert len(issued) == 2
    with store.connect() as db:
        active = db.execute(
            "SELECT code_hash FROM recovery_codes WHERE user_id=? AND used_at IS NULL",
            (user["id"],),
        ).fetchall()
        assert len(active) == 1
        assert active[0]["code_hash"] in {hash_token(value) for value in issued}
        assert db.execute(
            "SELECT COUNT(*) FROM audit_events WHERE action='user.recovery_code.rotate'"
        ).fetchone()[0] == 2


def test_readiness_reports_missing_frontend_without_leaking_paths(tmp_path: Path):
    deployment = DeploymentConfig(min_free_bytes=0)
    app = create_app(tmp_path, tmp_path / "viewer", start_worker=False, multi_user=True, deployment_config=deployment)
    try:
        ready, payload = app.readiness()
        assert ready is False
        assert payload["checks"]["frontend"] is False
        assert str(tmp_path) not in json.dumps(payload)
    finally:
        app.close()


def test_registration_invite_is_email_bound_hashed_and_single_use(tmp_path: Path):
    store = PlatformStore(tmp_path)
    store.register("owner@example.com", "Owner", "correct-horse-123", first_user_only=True)
    invite, token = store.create_registration_invite("member@example.com", hours=24)
    with store.connect() as db:
        row = db.execute("SELECT * FROM account_registration_invites WHERE id=?", (invite["id"],)).fetchone()
        assert row["token_hash"] != token
        assert row["email"] == "member@example.com"
    with pytest.raises(PermissionError, match="invalid or expired"):
        store.register(
            "other@example.com", "Other", "correct-horse-123", invite_token=token,
        )
    user, _recovery = store.register(
        "member@example.com", "Member", "correct-horse-123", invite_token=token,
    )
    assert user["role"] == "user"
    with pytest.raises(PermissionError, match="invalid or expired"):
        store.register(
            "member@example.com", "Member", "correct-horse-123", invite_token=token,
        )


def test_capacity_rejection_and_server_close_wait_for_active_request(capsys):
    started = threading.Event()
    release = threading.Event()

    class BlockingHandler(BaseHTTPRequestHandler):
        def log_message(self, _format, *_args):
            return

        def do_GET(self):
            started.set()
            assert release.wait(timeout=5)
            self.send_response(200)
            self.send_header("Content-Length", "2")
            self.end_headers()
            self.wfile.write(b"ok")

    server = BoundedThreadingHTTPServer(
        ("127.0.0.1", 0), BlockingHandler, max_concurrent=1, request_timeout=5,
        strict_transport_security=True,
    )
    server.daemon_threads = False
    server.block_on_close = True
    serving = threading.Thread(target=server.serve_forever)
    serving.start()
    first_result: list[int] = []

    def first_request():
        connection = http.client.HTTPConnection(*server.server_address, timeout=5)
        connection.request("GET", "/")
        response = connection.getresponse()
        response.read()
        first_result.append(response.status)
        connection.close()

    first = threading.Thread(target=first_request)
    first.start()
    assert started.wait(timeout=2)
    second = http.client.HTTPConnection(*server.server_address, timeout=2)
    second.request("GET", "/")
    response = second.getresponse()
    assert response.status == 503
    assert response.getheader("Retry-After") == "1"
    assert len(response.getheader("X-Request-ID")) == 32
    assert response.getheader("Strict-Transport-Security") == "max-age=31536000"
    response.read()
    second.close()

    server.shutdown()
    serving.join(timeout=2)
    closing = threading.Thread(target=server.server_close)
    closing.start()
    time.sleep(0.05)
    assert closing.is_alive()
    release.set()
    first.join(timeout=2)
    closing.join(timeout=2)
    assert first_result == [200]
    assert not closing.is_alive()
    assert '"event": "capacity_rejected"' in capsys.readouterr().out


def test_empty_database_invitation_cannot_create_first_admin_even_concurrently(tmp_path: Path):
    store = PlatformStore(tmp_path)
    _invite, token = store.create_registration_invite("member@example.com", hours=24)
    barrier = threading.Barrier(3)
    results: list[str] = []

    def register():
        barrier.wait()
        try:
            store.register(
                "member@example.com", "Member", "correct-horse-123", invite_token=token,
            )
            results.append("created")
        except PermissionError as exc:
            results.append(str(exc))

    threads = [threading.Thread(target=register) for _ in range(2)]
    for thread in threads:
        thread.start()
    barrier.wait()
    for thread in threads:
        thread.join(timeout=3)
    assert results == ["administrator bootstrap is required before invitation registration"] * 2
    assert store.user_count() == 0
    with store.connect() as db:
        assert db.execute("SELECT status FROM account_registration_invites").fetchone()[0] == "pending"


def test_security_rate_limit_rows_are_pruned_after_retention(tmp_path: Path):
    store = PlatformStore(tmp_path)
    now = int(time.time())
    with store.connect() as db:
        db.execute("INSERT INTO rate_limits VALUES('stale-rate',?,1,'2000-01-01T00:00:00+00:00')", (now - 8 * 86400,))
        db.execute("INSERT INTO rate_limits VALUES('fresh-rate',?,1,'2999-01-01T00:00:00+00:00')", (now,))
        db.execute("INSERT INTO login_attempts VALUES('stale-login',1,NULL,'2000-01-01T00:00:00+00:00')")
        db.execute("INSERT INTO login_attempts VALUES('fresh-login',1,NULL,'2999-01-01T00:00:00+00:00')")
    assert store.consume_rate_limit("maintenance-trigger", limit=5, window_seconds=60, now=now) == 0
    with store.connect() as db:
        assert db.execute("SELECT scope_hash FROM rate_limits WHERE scope_hash='stale-rate'").fetchone() is None
        assert db.execute("SELECT scope_hash FROM login_attempts WHERE scope_hash='stale-login'").fetchone() is None
        assert db.execute("SELECT scope_hash FROM rate_limits WHERE scope_hash='fresh-rate'").fetchone() is not None
        assert db.execute("SELECT scope_hash FROM login_attempts WHERE scope_hash='fresh-login'").fetchone() is not None
