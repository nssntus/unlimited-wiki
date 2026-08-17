#!/usr/bin/env python3
"""Secure local HTTP application for the Markdown knowledge workspace."""

from __future__ import annotations

import base64
import binascii
import contextlib
import hashlib
import hmac
import io
import json
import mimetypes
import os
import re
import signal
import stat
import shutil
import socket
import sys
import threading
import time
import traceback
import uuid
import zipfile
from http.cookies import SimpleCookie
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

import categories as cats
import keywords as kw
import websearch
import wiki_ops
from backup_restore import InstanceLock, instance_lock_path, restore_in_progress
from document_ingest import MAX_INPUT_BYTES
from deployment import DeploymentConfig, config_from_environment
from model_settings import build_config, load_model_settings, public_model_settings, save_model_settings
from legacy_migration import migrate_legacy_workspace
from platform_store import (
    AccountSessionContext,
    AccountWorkspaceSetChanged,
    PlatformIdempotencyError,
    PlatformStore,
    SessionContext,
)
from platform_review import PlatformReviewWorker
from publication import public_markdown, snapshot_fingerprint
from state_store import StateStore
from wiki_service import LLMConfig, WikiService, article_summary

ROOT = Path(__file__).resolve().parent
HOST = "127.0.0.1"
PORT = 8765
MAX_BODY = 64 * 1024
MAX_UPLOAD_BODY = ((MAX_INPUT_BYTES + 2) // 3) * 4 + 8 * 1024
CSP = (
    "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
    "img-src 'self'; font-src 'self'; connect-src 'self'; object-src 'none'; "
    "base-uri 'none'; frame-ancestors 'none'; form-action 'self'; worker-src 'none'"
)


class ApiError(RuntimeError):
    def __init__(self, status: int, message: str, *, details: dict | None = None):
        super().__init__(message)
        self.status = status
        self.details = details or {}


def load_dotenv(project_root: Path = ROOT) -> bool:
    path = project_root / ".env"
    if not path.exists():
        return True
    mode = stat.S_IMODE(path.stat().st_mode)
    permissions_ok = mode & 0o077 == 0
    if not permissions_ok:
        print(f"Warning: {path} permissions are too broad; run chmod 600 {path}", file=sys.stderr)
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))
    return permissions_ok


def env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


class DiagnosticCache:
    def __init__(self, service: WikiService):
        self.service = service
        self._lock = threading.Lock()
        self._values = {"todo": [], "lint": []}
        self._scanning = False
        self.refresh()

    def refresh(self) -> None:
        with self._lock:
            if self._scanning:
                return
            self._scanning = True
        threading.Thread(target=self._run, name="wiki-diagnostics", daemon=True).start()

    def _run(self) -> None:
        try:
            values = {
                "todo": wiki_ops.mentioned_but_missing(self.service.root),
                "lint": wiki_ops.lint_wiki(self.service.root),
            }
            with self._lock:
                self._values = values
        finally:
            with self._lock:
                self._scanning = False

    def get(self, key: str) -> dict:
        with self._lock:
            return {"items": list(self._values[key]), "scanning": self._scanning}


@dataclass
class WikiApp:
    project_root: Path
    viewer_dir: Path
    service: WikiService | None
    env_permissions_ok: bool = True
    dev_origins: set[str] = field(default_factory=set)
    platform: PlatformStore | None = None
    start_worker: bool = True
    remote_search: object = None
    remote_task_kinds: set[str] | None = None
    platform_reviewer: object = None
    deployment: DeploymentConfig = field(default_factory=DeploymentConfig)

    def __post_init__(self) -> None:
        self.project_root = self.project_root.resolve()
        self.viewer_dir = self.viewer_dir.resolve()
        self.dist_dir = (self.viewer_dir / "dist").resolve()
        self._service_lock = threading.RLock()
        self._workspace_gates_lock = threading.Lock()
        self._workspace_gates: dict[str, threading.RLock] = {}
        self._services: dict[str, WikiService] = {}
        self._diagnostics: dict[str, DiagnosticCache] = {}
        self._publication_backfills: set[tuple[str, str]] = set()
        if self.service is not None:
            self.diagnostics = DiagnosticCache(self.service)
        if self.platform is not None:
            self._reconcile_workspace_storage_states()
        self.review_worker = PlatformReviewWorker(self.platform, self.platform_reviewer) if self.platform is not None and self.start_worker else None

    @property
    def multi_user(self) -> bool:
        return self.platform is not None

    @property
    def registration_enabled(self) -> bool:
        if self.platform is None:
            return False
        mode = self.deployment.registration_mode
        return mode in {"open", "invite"} or (mode == "bootstrap" and self.platform.user_count() == 0)

    def readiness(self) -> tuple[bool, dict]:
        data_roots = [self.project_root / ".platform", self.project_root / "spaces"]

        def writable(path: Path) -> bool:
            probe = path / f".ready-{uuid.uuid4().hex}"
            try:
                path.mkdir(mode=0o700, parents=True, exist_ok=True)
                descriptor = os.open(probe, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                os.close(descriptor)
                probe.unlink()
                return True
            except OSError:
                with contextlib.suppress(OSError):
                    probe.unlink()
                return False

        checks = {
            "frontend": self.dist_dir.is_dir() and (self.dist_dir / "index.html").is_file(),
            "storage": all(writable(path) for path in data_roots),
            "database": True,
            "vault": True,
            "disk": all(
                shutil.disk_usage(path).free >= self.deployment.min_free_bytes
                for path in {item.resolve() for item in data_roots}
            ),
        }
        if self.platform is not None:
            try:
                with self.platform.connect() as db:
                    db.execute("PRAGMA busy_timeout=1000")
                    db.execute("BEGIN IMMEDIATE")
                    db.execute("UPDATE rate_limits SET updated_at=updated_at WHERE 0")
                    db.rollback()
            except Exception:
                checks["database"] = False
            try:
                checks["vault"] = self.platform.vault.path.stat().st_size == 32
            except OSError:
                checks["vault"] = False
        return all(checks.values()), {"status": "ready" if all(checks.values()) else "not_ready", "checks": checks}

    def workspace_service(self, context: SessionContext) -> WikiService:
        if self.platform is None:
            if self.service is None:
                raise RuntimeError("workspace service is unavailable")
            return self.service
        with self._workspace_gate(context.workspace_id):
            workspace = self.platform.authorize_workspace(context.user_id, context.workspace_id, "wiki.read")
            with self._service_lock:
                service = self._services.get(context.workspace_id)
            if service is None:
                values = self.platform.load_model(context.workspace_id)
                config = build_config(**values, allow_private=False) if values else LLMConfig()
                service = WikiService(
                    self.platform.workspace_root(workspace["root_name"]),
                    llm_config=config,
                    remote_search=self.remote_search,
                    start_worker=self.start_worker,
                    remote_task_kinds=self.remote_task_kinds,
                    authorize_actor=lambda user_id, workspace_id=context.workspace_id: bool(
                        self.platform.authorize_workspace(user_id, workspace_id, "wiki.write")
                    ),
                    actor_guard=lambda user_id, workspace_id=context.workspace_id: (
                        self.platform.authorized_workspace_action(user_id, workspace_id, "wiki.write")
                    ),
                    require_task_actor=True,
                )
                with self._service_lock:
                    self._services[context.workspace_id] = service
            backfill_key = (context.workspace_id, context.user_id)
            with self._service_lock:
                needs_backfill = backfill_key not in self._publication_backfills
            if needs_backfill:
                self.platform.backfill_workspace_publication_sources(context, service.articles())
                with self._service_lock:
                    self._publication_backfills.add(backfill_key)
            return service

    def _workspace_gate(self, workspace_id: str) -> threading.RLock:
        with self._workspace_gates_lock:
            return self._workspace_gates.setdefault(workspace_id, threading.RLock())

    @contextlib.contextmanager
    def workspace_action(self, context: SessionContext, service: WikiService, permission: str):
        if self.platform is None:
            with service.intent_guard():
                yield self.diagnostics
            return
        with self._workspace_gate(context.workspace_id):
            self.platform.authorize_workspace(context.user_id, context.workspace_id, permission)
            with self._service_lock:
                if self._services.get(context.workspace_id) is not service:
                    raise FileNotFoundError(context.workspace_id)
                if service.retired:
                    raise FileNotFoundError(context.workspace_id)
                diagnostics = self._diagnostics.get(context.workspace_id)
                if diagnostics is None:
                    diagnostics = DiagnosticCache(service)
                    self._diagnostics[context.workspace_id] = diagnostics
            if permission == "wiki.read":
                yield diagnostics
                return
            with service.intent_guard():
                if service.retired:
                    raise FileNotFoundError(context.workspace_id)
                with self.platform.authorized_workspace_action(
                    context.user_id, context.workspace_id, permission,
                ):
                    if service.retired:
                        raise FileNotFoundError(context.workspace_id)
                    yield diagnostics

    def diagnostics_for(self, context: SessionContext | None, service: WikiService) -> DiagnosticCache:
        if context is None:
            return self.diagnostics
        with self._service_lock:
            if service.retired or self._services.get(context.workspace_id) is not service:
                raise FileNotFoundError(context.workspace_id)
            cache = self._diagnostics.get(context.workspace_id)
            if cache is None:
                cache = DiagnosticCache(service)
                self._diagnostics[context.workspace_id] = cache
            return cache

    def _reconcile_workspace_storage(self, record: dict, service: WikiService | None = None) -> None:
        status = record["status"]
        if service is not None:
            service.reconcile_workspace_status(status)
            return
        root = self.platform.workspace_root(record["root_name"])
        if not root.is_dir():
            return
        state = StateStore(root, recover_running=status == "active")
        if status == "active":
            state.resume_paused_tasks()
        elif status == "suspended":
            state.pause_active_tasks()
        elif status == "deleted":
            state.terminate_workspace_tasks()

    def _reconcile_workspace_storage_states(self) -> None:
        for record in self.platform.workspace_storage_states():
            self._reconcile_workspace_storage(record)

    def run_workspace_lifecycle(self, user_id: str, workspace_id: str, transition) -> tuple[dict, bool]:
        if self.platform is None:
            raise RuntimeError("workspace lifecycle is unavailable")
        close_service: WikiService | None = None
        try:
            with self._workspace_gate(workspace_id):
                with self._service_lock:
                    service = self._services.get(workspace_id)
                guard = service.intent_guard() if service is not None else contextlib.nullcontext()
                with guard:
                    response, replay = transition()
                    record = self.platform.workspace_storage_state(workspace_id)
                    if service is not None and record["status"] != "active":
                        with self._service_lock:
                            self._services.pop(workspace_id, None)
                            self._diagnostics.pop(workspace_id, None)
                            self._publication_backfills = {
                                key for key in self._publication_backfills if key[0] != workspace_id
                            }
                        close_service = service
                    self._reconcile_workspace_storage(record, service)
                    response = self.platform.workspace_summary_for_user(user_id, workspace_id)
                return response, replay
        finally:
            if close_service is not None:
                close_service.close()

    def run_workspace_access_mutation(self, workspace_id: str, action):
        with self._workspace_gate(workspace_id):
            return action()

    def close(self) -> None:
        if self.service is not None:
            self.service.close()
        for service in list(self._services.values()):
            service.close()
        if self.review_worker is not None:
            self.review_worker.close()

    def export_workspace(self, context: SessionContext) -> bytes:
        workspace = self.platform.authorize_workspace(context.user_id, context.workspace_id, "workspace.manage")
        root = self.platform.workspace_root(workspace["root_name"])
        output = io.BytesIO()
        with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            for directory in ("wiki", "raw"):
                for path in sorted((root / directory).rglob("*")):
                    if path.is_file() and not path.is_symlink():
                        archive.write(path, path.relative_to(root).as_posix())
        return output.getvalue()

    def delete_account(self, context: SessionContext | AccountSessionContext, password: str) -> None:
        workspace_ids = set(self.platform.account_workspace_ids(context.user_id))
        while True:
            with contextlib.ExitStack() as gates:
                for workspace_id in sorted(workspace_ids):
                    gates.enter_context(self._workspace_gate(workspace_id))
                try:
                    result = self.platform.delete_account(
                        context, password, expected_workspace_ids=workspace_ids,
                    )
                except AccountWorkspaceSetChanged as exc:
                    workspace_ids.update(exc.workspace_ids)
                    continue
                cleanup_ids = {workspace["id"] for workspace in result["cleanup_workspaces"]}
                services: list[WikiService] = []
                with self._service_lock:
                    for workspace_id in cleanup_ids:
                        service = self._services.get(workspace_id)
                        if service is not None:
                            self._services.pop(workspace_id, None)
                            services.append(service)
                        self._diagnostics.pop(workspace_id, None)
                    self._publication_backfills = {
                        key for key in self._publication_backfills if key[0] not in cleanup_ids
                    }
                for service in services:
                    service.close()
                for workspace in result["cleanup_workspaces"]:
                    root = self.platform.workspace_root(workspace["root_name"])
                    if root.exists():
                        shutil.rmtree(root)
                return

    def status(self, service: WikiService) -> dict:
        config = service.llm
        return {
            "configured": config.configured,
            "model": config.model or None,
            "provider": config.provider or None,
            "env_permissions_ok": self.env_permissions_ok,
            "private_llm_allowed": config.allow_private,
            "insecure_http_llm_allowed": config.allow_insecure_http,
            "web_fake_ip_allowed": websearch.FETCHER.allow_fake_ip,
            "network": "available",
            "queue": {"active": sum(task["status"] in {"queued", "running"} for task in service.state.list_tasks())},
        }

    def model_settings(self, service: WikiService) -> dict:
        return public_model_settings(service.llm)

    def save_model_settings(self, service: WikiService, context: SessionContext | None, provider: str, base_url: str, api_key: str, model: str) -> dict:
        current = service.llm
        effective_key = api_key or (current.api_key if current.provider == provider and current.base_url == base_url.rstrip("/") else "")
        config = build_config(provider, base_url, effective_key, model, allow_private=not self.multi_user)
        if self.platform is not None and context is not None:
            self.platform.authorize_workspace(context.user_id, context.workspace_id, "model.manage")
            self.platform.save_model(context.workspace_id, provider, config.base_url, effective_key, model)
        else:
            save_model_settings(self.project_root, config)
        service.configure_llm(config)
        return public_model_settings(config)

    def list_models(self, provider: str, base_url: str, api_key: str) -> dict:
        if self.service is None:
            raise RuntimeError("workspace service is unavailable")
        return self.list_models_for(self.service, provider, base_url, api_key)

    def list_models_for(self, service: WikiService, provider: str, base_url: str, api_key: str) -> dict:
        current = service.llm
        effective_key = api_key or (current.api_key if current.provider == provider and current.base_url == base_url.rstrip("/") else "")
        config = build_config(provider, base_url, effective_key, "", require_model=False, allow_private=not self.multi_user)
        try:
            from openai import OpenAI
        except ImportError as exc:
            raise ValueError("OpenAI-compatible client is not installed") from exc
        try:
            response = OpenAI(
                api_key=config.api_key or "not-needed",
                base_url=config.base_url,
                timeout=15,
                max_retries=0,
            ).models.list()
            models = sorted({item.id for item in response.data if isinstance(item.id, str) and item.id})
        except Exception as exc:
            raise ValueError("Unable to load models; check the Base URL, API key, and provider availability") from exc
        return {"models": models[:500]}


def create_app(
    project_root: Path = ROOT,
    viewer_dir: Path | None = None,
    *,
    llm_config: LLMConfig | None = None,
    remote_search=None,
    start_worker: bool = True,
    remote_task_kinds: set[str] | None = None,
    load_environment: bool = False,
    dev_origins: set[str] | None = None,
    multi_user: bool = False,
    platform_reviewer=None,
    deployment_config: DeploymentConfig | None = None,
) -> WikiApp:
    root = Path(project_root).resolve()
    permissions_ok = load_dotenv(root) if load_environment else True
    websearch.configure_network(
        allow_fake_ip=load_environment and env("WEB_ALLOW_FAKE_IP").lower() in {"1", "true", "yes"},
    )
    service = None if multi_user else WikiService(
        root,
        llm_config=llm_config or load_model_settings(root),
        remote_search=remote_search,
        start_worker=start_worker,
        remote_task_kinds=remote_task_kinds,
    )
    platform = PlatformStore(root) if multi_user else None
    return WikiApp(
        project_root=root,
        viewer_dir=Path(viewer_dir or root / "viewer"),
        service=service,
        env_permissions_ok=permissions_ok,
        dev_origins=dev_origins or set(),
        platform=platform,
        start_worker=start_worker,
        remote_search=remote_search,
        remote_task_kinds=remote_task_kinds,
        platform_reviewer=platform_reviewer,
        deployment=deployment_config or DeploymentConfig(),
    )


def _strict_json(raw: bytes) -> dict:
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ApiError(400, f"duplicate JSON field: {key}")
            result[key] = value
        return result

    try:
        value = json.loads(raw.decode("utf-8"), object_pairs_hook=pairs)
    except UnicodeDecodeError as exc:
        raise ApiError(400, "request body must be UTF-8") from exc
    except json.JSONDecodeError as exc:
        raise ApiError(400, "request body is not valid JSON") from exc
    if not isinstance(value, dict):
        raise ApiError(400, "request JSON must be an object")
    return value


def _fields(data: dict, allowed: set[str], required: set[str] = frozenset()) -> None:
    unknown = set(data) - allowed
    missing = required - set(data)
    if unknown:
        raise ApiError(400, f"unknown fields: {', '.join(sorted(unknown))}")
    if missing:
        raise ApiError(422, f"missing fields: {', '.join(sorted(missing))}")


def _string(data: dict, key: str, *, maximum: int | None, required: bool = False) -> str:
    value = data.get(key)
    if value is None and not required:
        return ""
    if not isinstance(value, str):
        raise ApiError(422, f"{key} must be a string")
    value = value.strip() if key not in {"markdown", "passage", "content"} else value
    if required and not value:
        raise ApiError(422, f"{key} is required")
    if maximum is not None and len(value) > maximum:
        raise ApiError(422, f"{key} exceeds {maximum} characters")
    return value


def _single_query(parsed, key: str, *, required: bool = False) -> str:
    values = parse_qs(parsed.query, keep_blank_values=True).get(key, [])
    if len(values) > 1:
        raise ApiError(400, f"duplicate query parameter: {key}")
    value = values[0] if values else ""
    if required and not value:
        raise ApiError(422, f"missing query parameter: {key}")
    return value


def _search(service: WikiService, query: str) -> list[dict]:
    needle = query.casefold().strip()
    if not needle:
        return []
    rows = []
    for item in service.articles():
        markdown = (service.root / "wiki" / item["path"]).read_text(encoding="utf-8")
        haystack = " ".join([item["title"], *item["aliases"], item["category_label"], markdown]).casefold()
        if needle not in haystack:
            continue
        position = markdown.casefold().find(needle)
        snippet = ""
        if position >= 0:
            snippet = re.sub(r"\s+", " ", markdown[max(0, position - 80):position + len(query) + 120])
        rows.append({**item, "snippet": snippet})
    return rows[:100]


def make_handler(app: WikiApp):
    class Handler(BaseHTTPRequestHandler):
        server_version = "LLMWiki/2"
        sys_version = ""

        context: SessionContext | None = None
        account_context: AccountSessionContext | None = None
        service: WikiService | None = None
        diagnostics: DiagnosticCache | None = None
        _workspace_action: object | None = None
        _extra_headers: list[tuple[str, str]]

        def log_message(self, fmt: str, *args) -> None:
            return

        def _begin_request(self) -> None:
            self._request_started = time.monotonic()
            self._request_id = uuid.uuid4().hex
            self._response_status = 0
            self._response_bytes = 0
            self._extra_headers = []

        def _finish_request(self) -> None:
            path = urlparse(self.path).path
            path = re.sub(r"/[a-f0-9]{32}(?=/|$)", "/{id}", path)
            try:
                client_ip = self._client_ip()
            except ValueError:
                client_ip = self.client_address[0]
            print(json.dumps({
                "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "request_id": self._request_id,
                "client_ip": client_ip,
                "method": self.command,
                "path": path,
                "status": self._response_status or 500,
                "duration_ms": round((time.monotonic() - self._request_started) * 1000, 2),
                "response_bytes": self._response_bytes,
            }, ensure_ascii=True), flush=True)

        def _headers(self, code: int, content_type: str, length: int, *, cache: str = "no-store") -> None:
            self._response_status = code
            self._response_bytes = length
            self.send_response(code)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(length))
            self.send_header("Cache-Control", cache)
            self.send_header("Content-Security-Policy", CSP)
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
            self.send_header("Cross-Origin-Opener-Policy", "same-origin")
            self.send_header("Cross-Origin-Resource-Policy", "same-origin")
            self.send_header("X-Request-ID", self._request_id)
            if app.deployment.lan_mode:
                self.send_header("Strict-Transport-Security", "max-age=31536000")
            for key, value in getattr(self, "_extra_headers", []):
                self.send_header(key, value)
            self.end_headers()

        def _send(self, code: int, body: bytes, content_type: str, *, cache: str = "no-store") -> None:
            self._headers(code, content_type, len(body), cache=cache)
            if self.command != "HEAD":
                self.wfile.write(body)

        def _json(self, code: int, payload: object) -> None:
            self._send(code, json.dumps(payload, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8")

        def _error(self, error: ApiError) -> None:
            self._json(error.status, {"error": str(error), **error.details})

        def _session_token(self) -> str:
            cookie = SimpleCookie()
            try:
                cookie.load(self.headers.get("Cookie", ""))
            except Exception:
                return ""
            morsel = cookie.get(app.deployment.cookie_name)
            return morsel.value if morsel else ""

        def _set_session_cookie(self, token: str, *, clear: bool = False) -> None:
            value = "" if clear else token
            age = 0 if clear else 12 * 60 * 60
            cookie = f"{app.deployment.cookie_name}={value}; Path=/; HttpOnly; SameSite=Strict; Max-Age={age}"
            if app.deployment.lan_mode or env("WIKI_SECURE_COOKIES").lower() in {"1", "true", "yes"}:
                cookie += "; Secure"
            self._extra_headers.append(("Set-Cookie", cookie))

        def _prepare_context(self, *, required: bool) -> None:
            self._extra_headers = []
            if app.platform is None:
                self.service = app.service
                self.diagnostics = app.diagnostics
                return
            token = self._session_token()
            self.account_context = app.platform.resolve_account_session(token)
            if self.account_context is None:
                if required:
                    raise ApiError(401, "authentication required")
                return
            self.context = app.platform.resolve_session(token)
            if self.context is None:
                return
            self.service = app.workspace_service(self.context)

        def _require_csrf(self) -> None:
            if app.platform is None:
                return
            if self.account_context is None or not hmac.compare_digest(self.headers.get("X-CSRF-Token", ""), self.account_context.csrf_token):
                raise ApiError(403, "invalid CSRF token")

        def _require_role(self, role: str) -> None:
            if self.account_context is None:
                raise ApiError(401, "authentication required")
            if self.account_context.role != role:
                raise ApiError(403, "insufficient role")

        def _require_workspace_permission(self, permission: str) -> None:
            if app.platform is None:
                return
            if self.context is None:
                raise ApiError(401, "authentication required")
            try:
                app.platform.authorize_workspace(self.context.user_id, self.context.workspace_id, permission)
            except FileNotFoundError as exc:
                raise ApiError(404, "not found") from exc
            except PermissionError as exc:
                raise ApiError(403, "insufficient workspace role") from exc

        def _private_service(self) -> WikiService:
            if self.service is None:
                if self.account_context is not None:
                    raise ApiError(409, "workspace selection required", details={"code": "workspace_selection_required"})
                raise ApiError(401, "authentication required")
            return self.service

        def _enter_workspace_action(self, permission: str) -> None:
            if app.platform is None:
                return
            if self.context is None or self.service is None:
                raise ApiError(409, "workspace selection required", details={"code": "workspace_selection_required"})
            action = app.workspace_action(self.context, self.service, permission)
            try:
                self.diagnostics = action.__enter__()
            except FileNotFoundError as exc:
                raise ApiError(409, "workspace selection required", details={"code": "workspace_selection_required"}) from exc
            self._workspace_action = action

        def _release_workspace_action(self) -> None:
            action = self._workspace_action
            self._workspace_action = None
            if action is not None:
                action.__exit__(None, None, None)

        def _client_ip(self) -> str:
            return app.deployment.client_ip(
                self.client_address[0], self.headers.get("X-Forwarded-For"),
            )

        def _trusted_host(self) -> bool:
            return app.deployment.trusted_host(
                self.headers.get("Host", ""), self.server.server_address[1],
            )

        def _validate_host(self) -> None:
            try:
                app.deployment.validate_forwarded_proto(
                    self.client_address[0], self.headers.get("X-Forwarded-Proto"),
                )
                self._client_ip()
            except ValueError as exc:
                raise ApiError(400, str(exc)) from exc
            if not self._trusted_host():
                raise ApiError(403, "untrusted Host header")

        def _validate_origin(self) -> None:
            if not app.deployment.trusted_origin(
                self.headers.get("Origin"), self.server.server_address[1], app.dev_origins,
            ):
                raise ApiError(403, "untrusted or missing Origin header")

        def _rate_limit(
            self, scope: str, *, limit: int, window_seconds: int, consume: bool = True,
        ) -> None:
            if app.platform is None:
                return
            retry_after = app.platform.consume_rate_limit(
                scope, limit=limit, window_seconds=window_seconds, consume=consume,
            )
            if retry_after:
                self._extra_headers.append(("Retry-After", str(retry_after)))
                raise ApiError(429, "too many requests; try again later")

        def _read_json(self, *, max_bytes: int | None = MAX_BODY) -> dict:
            if self.headers.get("Transfer-Encoding"):
                raise ApiError(400, "Transfer-Encoding is not supported")
            raw_length = self.headers.get("Content-Length")
            if raw_length is None:
                raise ApiError(411, "Content-Length is required")
            if not raw_length.isdecimal():
                raise ApiError(400, "Content-Length must be a non-negative integer")
            length = int(raw_length)
            if max_bytes is not None and length > max_bytes:
                self.close_connection = True
                raise ApiError(413, f"request body exceeds {max_bytes} bytes")
            media_type, _, charset = self.headers.get("Content-Type", "").partition(";")
            if media_type.strip().lower() != "application/json":
                raise ApiError(415, "Content-Type must be application/json")
            if charset and charset.strip().lower() not in {"charset=utf-8", "charset=\"utf-8\""}:
                raise ApiError(415, "JSON charset must be UTF-8")
            deadline = time.monotonic() + app.deployment.request_timeout_seconds
            chunks = []
            received = 0
            try:
                while received < length:
                    remaining_seconds = deadline - time.monotonic()
                    if remaining_seconds <= 0:
                        raise ApiError(408, "request body deadline exceeded")
                    self.connection.settimeout(remaining_seconds)
                    try:
                        chunk = self.rfile.read1(min(64 * 1024, length - received))
                    except socket.timeout as exc:
                        raise ApiError(408, "request body deadline exceeded") from exc
                    if not chunk:
                        break
                    chunks.append(chunk)
                    received += len(chunk)
            finally:
                self.connection.settimeout(app.deployment.request_timeout_seconds)
            raw = b"".join(chunks)
            if len(raw) != length:
                raise ApiError(400, "request body was truncated")
            return _strict_json(raw)

        def _idempotency(self, data: dict, action):
            key = self.headers.get("Idempotency-Key", "")
            if not key or len(key) > 128 or not key.isascii() or any(ord(ch) < 33 for ch in key):
                raise ApiError(422, "a printable ASCII Idempotency-Key is required")
            endpoint = urlparse(self.path).path
            try:
                replay = self._private_service().state.idempotency_get(endpoint, key, data)
            except ValueError as exc:
                raise ApiError(409, str(exc)) from exc
            except RuntimeError as exc:
                raise ApiError(409, str(exc)) from exc
            if replay is not None:
                return replay, True
            if not self._private_service().state.idempotency_begin(endpoint, key, data):
                try:
                    replay = self._private_service().state.idempotency_get(endpoint, key, data)
                except (ValueError, RuntimeError) as exc:
                    raise ApiError(409, str(exc)) from exc
                return replay, True
            try:
                response = action()
                self._private_service().state.idempotency_finish(endpoint, key, response)
                return response, False
            except BaseException:
                self._private_service().state.idempotency_abort(endpoint, key)
                raise

        def _platform_idempotency(self, data: dict, action, *, scope: str | None = None):
            if app.platform is None or self.account_context is None:
                raise ApiError(401, "authentication required")
            key = self.headers.get("Idempotency-Key", "")
            if not key or len(key) > 128 or not key.isascii() or any(ord(ch) < 33 for ch in key):
                raise ApiError(422, "a printable ASCII Idempotency-Key is required")
            endpoint = urlparse(self.path).path
            if scope is None:
                if self.context is None:
                    raise ApiError(409, "workspace selection required", details={"code": "workspace_selection_required"})
                scope = f"workspace:{self.context.workspace_id}"
            try:
                return app.platform.run_platform_idempotent(scope, endpoint, key, data, action)
            except PlatformIdempotencyError as exc:
                raise ApiError(409, str(exc)) from exc

        def _serve_static(self, path: str) -> None:
            dist = app.dist_dir
            if not dist.is_dir():
                raise ApiError(503, "frontend build is missing; run npm run build in viewer/")
            rel = path.lstrip("/") or "index.html"
            if rel.startswith("api/") or any(part.startswith(".") for part in Path(rel).parts):
                raise ApiError(404, "not found")
            target = (dist / rel).resolve()
            if not target.is_relative_to(dist) or not target.is_file():
                target = dist / "index.html"
            media_type = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
            if target.suffix in {".html", ".js", ".css", ".json", ".svg"} and media_type.startswith("text/") is False and target.suffix != ".js":
                media_type = {".json": "application/json", ".svg": "image/svg+xml"}.get(target.suffix, media_type)
            if target.suffix == ".js":
                media_type = "text/javascript"
            cache = "public, max-age=31536000, immutable" if "/assets/" in self.path else "no-store"
            self._send(200, target.read_bytes(), media_type, cache=cache)

        def _dispatch_get(self) -> None:
            parsed = urlparse(self.path)
            path = unquote(parsed.path)
            if path == "/healthz":
                return self._json(200, {"status": "ok"})
            if path == "/readyz":
                ready, payload = app.readiness()
                return self._json(200 if ready else 503, payload)
            if path == "/api/platform/config":
                return self._json(200, {
                    "registration_enabled": app.registration_enabled,
                    "registration_mode": app.deployment.registration_mode,
                    "public_square_enabled": True,
                })
            if path == "/api/auth/session":
                if self.account_context is None:
                    return self._json(200, {
                        "authenticated": False,
                        "registration_enabled": app.registration_enabled,
                        "registration_mode": app.deployment.registration_mode,
                    })
                public_context = self.context.public() if self.context is not None else self.account_context.public()
                return self._json(200, {
                    "authenticated": True,
                    **public_context,
                    "registration_enabled": app.registration_enabled,
                    "registration_mode": app.deployment.registration_mode,
                })
            if path == "/api/account/export":
                if self.context is None:
                    raise ApiError(401, "authentication required")
                self._require_workspace_permission("workspace.manage")
                self._enter_workspace_action("workspace.manage")
                payload = app.export_workspace(self.context)
                self._extra_headers.append(("Content-Disposition", "attachment; filename=wiki-export.zip"))
                return self._send(200, payload, "application/zip")
            if path == "/api/workspaces":
                if self.account_context is None:
                    raise ApiError(401, "authentication required")
                include_inactive = _single_query(parsed, "include_inactive") == "1"
                return self._json(200, app.platform.list_workspaces(
                    self.context or self.account_context, include_inactive=include_inactive,
                ))
            if path == "/api/invitations":
                if self.account_context is None:
                    raise ApiError(401, "authentication required")
                return self._json(200, app.platform.list_invitations(self.context or self.account_context))
            if path == "/api/workspace/members":
                if self.context is None:
                    raise ApiError(401, "authentication required")
                return self._json(200, app.platform.list_workspace_members(self.context))
            if path == "/api/public/entries":
                if app.platform is None:
                    return self._json(200, [])
                return self._json(200, app.platform.list_public(_single_query(parsed, "q"), _single_query(parsed, "category")))
            match = re.fullmatch(r"/api/public/entries/([a-f0-9]{32})", path)
            if match:
                if app.platform is None:
                    raise ApiError(404, "not found")
                return self._json(200, app.platform.get_public(match.group(1)))
            if not path.startswith("/api/"):
                return self._serve_static(path)

            service = self._private_service()
            permission = (
                "model.manage" if path == "/api/settings/model"
                else "wiki.write" if path in {"/api/ingest/preview", "/api/operations"}
                else "wiki.read"
            )
            self._enter_workspace_action(permission)
            if permission == "wiki.write" and self.context is not None:
                service.set_request_actor(self.context.user_id)
            if path == "/api/status":
                return self._json(200, app.status(service))
            if path == "/api/settings/model":
                self._require_workspace_permission("model.manage")
                return self._json(200, app.model_settings(service))
            if path == "/api/articles":
                return self._json(200, service.articles())
            if path == "/api/categories":
                status = _single_query(parsed, "status") or "active"
                if status not in {"active", "archived", "all"}:
                    raise ApiError(400, "invalid category status")
                rows = service.categories()
                return self._json(200, rows if status == "all" else [item for item in rows if item["status"] == status])
            if path == "/api/classifications/workbench":
                return self._json(200, service.classification_workbench())
            if path == "/api/reconciliation":
                return self._json(200, {"items": service.state.list_reconciliation()})
            if path == "/api/keywords":
                return self._json(200, wiki_ops.highlightable_keywords(service.root))
            if path == "/api/todo":
                if _single_query(parsed, "refresh") == "1":
                    self.diagnostics.refresh()
                return self._json(200, self.diagnostics.get("todo"))
            if path == "/api/lint":
                if _single_query(parsed, "refresh") == "1":
                    self.diagnostics.refresh()
                return self._json(200, self.diagnostics.get("lint"))
            if path == "/api/article":
                article = service.read_article(_single_query(parsed, "path", required=True))
                article["publication"] = app.platform.article_publication(self.context, article) if app.platform and self.context else {
                    "state": "not_published", "public_entry_id": None, "public_revision_id": None,
                    "public_version": None, "published_at": None, "submission_id": None,
                    "submission_status": None, "submission_matches_current": False,
                    "publication_fingerprint": None,
                    "moderation_reason": None, "moderated_at": None,
                }
                return self._json(200, article)
            if path == "/api/raw":
                return self._json(200, service.read_raw(_single_query(parsed, "path", required=True)))
            if path == "/api/tasks":
                return self._json(200, service.state.list_tasks())
            if path.startswith("/api/tasks/"):
                return self._json(200, service.state.get_task(path.rsplit("/", 1)[-1]))
            if path == "/api/ingest":
                return self._json(200, service.raw_inbox())
            if path == "/api/ingest/preview":
                self._require_workspace_permission("wiki.write")
                return self._json(200, service.ingest_preview(_single_query(parsed, "path", required=True)))
            if path == "/api/merge/preview":
                return self._json(200, service.merge_preview(_single_query(parsed, "source", required=True), _single_query(parsed, "target", required=True)))
            if path == "/api/operations":
                self._require_workspace_permission("wiki.write")
                return self._json(200, service.files.operation(_single_query(parsed, "id", required=True)))
            if path == "/api/search":
                return self._json(200, _search(service, _single_query(parsed, "q")))
            if path == "/api/submissions":
                return self._json(200, app.platform.list_submissions(self.context))
            if path == "/api/notifications":
                return self._json(200, app.platform.list_notifications(self.context))
            match = re.fullmatch(r"/api/submissions/([a-f0-9]{32})", path)
            if match:
                return self._json(200, app.platform.get_submission(self.context, match.group(1)))
            if path == "/api/admin/submissions":
                self._require_role("admin")
                return self._json(200, app.platform.admin_list(self.context, _single_query(parsed, "status") or "pending_admin"))
            match = re.fullmatch(r"/api/admin/submissions/([a-f0-9]{32})", path)
            if match:
                self._require_role("admin")
                return self._json(200, app.platform.admin_get(self.context, match.group(1)))
            if path == "/api/admin/reports":
                self._require_role("admin")
                return self._json(200, app.platform.admin_reports(self.context))
            if path == "/api/admin/public-entries":
                self._require_role("admin")
                return self._json(200, app.platform.admin_public_entries(
                    self.context, _single_query(parsed, "status") or "published",
                ))
            return self._serve_static(path)

        def _dispatch_post(self) -> None:
            parsed = urlparse(self.path)
            path = parsed.path
            if path == "/api/article/save":
                max_bytes = None
            elif path == "/api/ingest/upload":
                max_bytes = MAX_UPLOAD_BODY
            else:
                max_bytes = MAX_BODY
            data = self._read_json(max_bytes=max_bytes)
            if path == "/api/auth/register":
                if app.platform is None:
                    raise ApiError(404, "not found")
                if app.deployment.registration_mode == "closed":
                    raise ApiError(403, "registration is closed")
                self._rate_limit(
                    f"register:ip:{self._client_ip()}", limit=5, window_seconds=3600,
                )
                allowed = {"email", "nickname", "password", "invite_token"}
                _fields(data, allowed, {"email", "nickname", "password"})
                invite_token = _string(data, "invite_token", maximum=256)
                if app.deployment.registration_mode == "invite" and not invite_token:
                    raise ApiError(422, "invite_token is required")
                user, recovery = app.platform.register(
                    _string(data, "email", maximum=254, required=True),
                    _string(data, "nickname", maximum=80, required=True),
                    _string(data, "password", maximum=1024, required=True),
                    first_user_only=app.deployment.registration_mode == "bootstrap",
                    invite_token=invite_token,
                )
                migration = migrate_legacy_workspace(app.platform, user["id"]) if user["role"] == "admin" else {"status": "not_needed"}
                token, context = app.platform.create_session(user["id"])
                self.context = context
                self._set_session_cookie(token)
                return self._json(201, {"authenticated": True, **context.public(), "recovery_code": recovery, "migration": {"status": migration["status"]}})
            if path == "/api/auth/login":
                if app.platform is None:
                    raise ApiError(404, "not found")
                _fields(data, {"email", "password"}, {"email", "password"})
                email = _string(data, "email", maximum=254, required=True).casefold()
                client_ip = self._client_ip()
                self._rate_limit(f"login:ip:{client_ip}", limit=30, window_seconds=900, consume=False)
                self._rate_limit(f"login:account:{email}", limit=10, window_seconds=900, consume=False)
                try:
                    user = app.platform.authenticate(
                        email,
                        _string(data, "password", maximum=1024, required=True),
                        remote=client_ip,
                    )
                except RuntimeError as exc:
                    self._extra_headers.append(("Retry-After", "900"))
                    raise ApiError(429, "too many login attempts; try again later") from exc
                if user is None:
                    self._rate_limit(f"login:ip:{client_ip}", limit=30, window_seconds=900)
                    self._rate_limit(f"login:account:{email}", limit=10, window_seconds=900)
                    raise ApiError(401, "email or password is invalid")
                token, context = app.platform.create_session(user["id"])
                self.context = context
                self._set_session_cookie(token)
                return self._json(200, {"authenticated": True, **context.public()})
            if path == "/api/auth/recover":
                if app.platform is None:
                    raise ApiError(404, "not found")
                _fields(data, {"email", "recovery_code", "new_password"}, {"email", "recovery_code", "new_password"})
                email = _string(data, "email", maximum=254, required=True).casefold()
                self._rate_limit(f"recover:ip:{self._client_ip()}", limit=5, window_seconds=3600)
                self._rate_limit(f"recover:account:{email}", limit=5, window_seconds=3600)
                ok = app.platform.reset_password(
                    email,
                    _string(data, "recovery_code", maximum=200, required=True),
                    _string(data, "new_password", maximum=1024, required=True),
                )
                if not ok:
                    raise ApiError(400, "recovery request is invalid or expired")
                return self._json(200, {"reset": True})
            match = re.fullmatch(r"/api/public/entries/([a-f0-9]{32})/reports", path)
            if match:
                if app.platform is None:
                    raise ApiError(404, "not found")
                self._rate_limit(f"report:ip:{self._client_ip()}", limit=30, window_seconds=3600)
                _fields(data, {"reason_code", "detail"}, {"reason_code"})
                response = app.platform.report_public(
                    match.group(1), self.account_context.user_id if self.account_context else None,
                    _string(data, "reason_code", maximum=40, required=True), _string(data, "detail", maximum=1000),
                )
                return self._json(201, response)

            if path == "/api/workspaces":
                _fields(data, {"display_name"}, {"display_name"})
                response, replay = self._platform_idempotency(data, lambda: app.platform.create_team(
                    self.context or self.account_context, _string(data, "display_name", maximum=80, required=True),
                ), scope=f"account:{self.account_context.user_id}")
                return self._json(200 if replay else 201, response)
            if path == "/api/workspaces/switch":
                _fields(data, {"workspace_id"}, {"workspace_id"})
                token = self._session_token()
                _, _ = self._platform_idempotency(data, lambda: {
                    "workspace_id": app.platform.switch_workspace(
                        token, self.context or self.account_context, _string(data, "workspace_id", maximum=64, required=True),
                    ).workspace_id,
                }, scope=f"session:{hashlib.sha256(token.encode('utf-8')).hexdigest()}")
                selected = app.platform.resolve_session(token)
                if selected is None:
                    raise ApiError(409, "workspace switch failed")
                self.context = selected
                return self._json(200, {"authenticated": True, **selected.public()})
            if path == "/api/workspace/invitations":
                _fields(data, {"email", "role"}, {"email", "role"})
                response, replay = self._platform_idempotency(data, lambda: app.platform.invite_workspace_member(
                    self.context,
                    _string(data, "email", maximum=254, required=True),
                    _string(data, "role", maximum=20, required=True),
                ))
                return self._json(200 if replay else 201, response)
            match = re.fullmatch(r"/api/invitations/([a-f0-9]{32})/(?P<action>accept|decline)", path)
            if match:
                _fields(data, set())
                response, _ = self._platform_idempotency(data, lambda: app.platform.respond_to_invitation(
                    self.context or self.account_context, match.group(1), accept=match.group("action") == "accept",
                ), scope=f"invitation:{self.account_context.user_id}:{match.group(1)}")
                if response.get("status") == "expired":
                    raise ApiError(422, "invitation is no longer available")
                return self._json(200, response)
            match = re.fullmatch(r"/api/workspace/members/([a-f0-9]{32})/(?P<action>role|remove)", path)
            if match:
                action = match.group("action")
                _fields(data, {"role"} if action == "role" else set(), {"role"} if action == "role" else set())
                response, _ = app.run_workspace_access_mutation(
                    self.context.workspace_id,
                    lambda: self._platform_idempotency(data, lambda: (
                        app.platform.change_workspace_member_role(
                            self.context, match.group(1), _string(data, "role", maximum=20, required=True),
                        ) if action == "role" else app.platform.remove_workspace_member(self.context, match.group(1))
                    )),
                )
                return self._json(200, response)
            if path == "/api/workspace/owner-transfer":
                _fields(data, {"user_id"}, {"user_id"})
                response, _ = app.run_workspace_access_mutation(
                    self.context.workspace_id,
                    lambda: self._platform_idempotency(data, lambda: app.platform.transfer_workspace_owner(
                        self.context, _string(data, "user_id", maximum=64, required=True),
                    )),
                )
                return self._json(200, response)
            if path == "/api/workspace/rename":
                _fields(data, {"display_name"}, {"display_name"})
                response, _ = self._platform_idempotency(data, lambda: app.platform.rename_workspace(
                    self.context, _string(data, "display_name", maximum=80, required=True),
                ))
                return self._json(200, response)

            match = re.fullmatch(r"/api/workspaces/([a-f0-9]{32})/(?P<action>suspend|restore|delete|leave)", path)
            if match:
                action = match.group("action")
                _fields(data, set())
                workspace_id = match.group(1)
                account = self.context or self.account_context
                transition = lambda: self._platform_idempotency(
                    data,
                    lambda: app.platform.change_workspace_lifecycle(account, workspace_id, action),
                    scope=f"account:{self.account_context.user_id}:workspace:{workspace_id}",
                )
                if action == "leave":
                    response, replay = app.run_workspace_access_mutation(
                        workspace_id,
                        lambda: self._platform_idempotency(
                            data, lambda: app.platform.leave_workspace(account, workspace_id),
                            scope=f"account:{self.account_context.user_id}:workspace:{workspace_id}",
                        ),
                    )
                else:
                    response, replay = app.run_workspace_lifecycle(account.user_id, workspace_id, transition)
                return self._json(200, response)

            if path == "/api/auth/logout":
                _fields(data, set())
                app.platform.revoke_session(self._session_token())
                self._set_session_cookie("", clear=True)
                return self._json(200, {"authenticated": False})
            if path == "/api/auth/sessions/revoke-all":
                _fields(data, set())
                app.platform.revoke_all_sessions(self.account_context.user_id)
                self._set_session_cookie("", clear=True)
                return self._json(200, {"authenticated": False})
            if path == "/api/account/delete":
                _fields(data, {"password"}, {"password"})
                app.delete_account(self.context or self.account_context, _string(data, "password", maximum=1024, required=True))
                self._set_session_cookie("", clear=True)
                return self._json(200, {"deleted": True})

            service = self._private_service()
            content_write_paths = {
                "/api/generate", "/api/meta", "/api/classifications/preview", "/api/classifications/draft",
                "/api/classifications/commit", "/api/classifications/retry", "/api/categories/preview",
                "/api/categories/commit", "/api/reconciliation/scan", "/api/reconciliation/preview",
                "/api/reconciliation/commit", "/api/governance", "/api/article/save", "/api/ingest/commit",
                "/api/ingest/upload", "/api/merge/commit", "/api/share-previews", "/api/submissions",
            }
            if path in {"/api/settings/models", "/api/settings/model"}:
                self._require_workspace_permission("model.manage")
                self._enter_workspace_action("model.manage")
            elif (
                path in content_write_paths
                or re.fullmatch(r"/api/tasks/[a-f0-9]+/(cancel|retry)", path)
                or re.fullmatch(r"/api/operations/[A-Za-z0-9-]+/rollback", path)
                or re.fullmatch(r"/api/submissions/[a-f0-9]{32}/(ai-retry|withdraw)", path)
            ):
                self._require_workspace_permission("wiki.write")
                self._enter_workspace_action("wiki.write")
            if path == "/api/settings/models":
                _fields(data, {"provider", "base_url", "api_key"}, {"provider", "base_url"})
                values = (
                    _string(data, "provider", maximum=40, required=True),
                    _string(data, "base_url", maximum=2048, required=True),
                    _string(data, "api_key", maximum=8192),
                )
                return self._json(200, app.list_models_for(service, *values) if app.multi_user else app.list_models(*values))
            if path == "/api/settings/model":
                _fields(data, {"provider", "base_url", "api_key", "model"}, {"provider", "base_url", "model"})
                provider = _string(data, "provider", maximum=40, required=True)
                base_url = _string(data, "base_url", maximum=2048, required=True)
                api_key = _string(data, "api_key", maximum=8192)
                model = _string(data, "model", maximum=200, required=True)
                fingerprint = hashlib.sha256(api_key.encode("utf-8")).hexdigest() if api_key else "preserve"
                safe_data = {"provider": provider, "base_url": base_url, "model": model, "api_key_fingerprint": fingerprint}
                response, _ = self._idempotency(safe_data, lambda: app.save_model_settings(service, self.context, provider, base_url, api_key, model))
                return self._json(200, response)
            if path == "/api/generate/preflight":
                _fields(data, {"keyword", "from_path", "heading", "passage"}, {"keyword"})
                result = service.preflight_generate(
                    _string(data, "keyword", maximum=120, required=True),
                    from_path=_string(data, "from_path", maximum=512),
                    heading=_string(data, "heading", maximum=200),
                    passage=_string(data, "passage", maximum=800),
                )
                return self._json(200, result)
            if path == "/api/generate":
                _fields(data, {"keyword", "from_path", "heading", "passage"}, {"keyword"})
                response, replay = self._idempotency(data, lambda: service.generate(
                    _string(data, "keyword", maximum=120, required=True),
                    from_path=_string(data, "from_path", maximum=512),
                    heading=_string(data, "heading", maximum=200),
                    passage=_string(data, "passage", maximum=800),
                ))
                self.diagnostics.refresh()
                return self._json(200 if replay else 202, response)
            if path == "/api/meta":
                _fields(data, {"path", "category", "status"}, {"path", "category", "status"})
                response, _ = self._idempotency(data, lambda: service.apply_meta(
                    _string(data, "path", maximum=512, required=True),
                    category=_string(data, "category", maximum=80, required=True),
                    status=_string(data, "status", maximum=20, required=True),
                ))
                self.diagnostics.refresh()
                return self._json(200, response)
            if path == "/api/classifications/preview":
                _fields(data, {"selections"}, {"selections"})
                selections = data.get("selections")
                if not isinstance(selections, list):
                    raise ApiError(422, "selections must be an array")
                return self._json(200, service.classification_preview(selections))
            if path == "/api/classifications/draft":
                _fields(data, {"selections", "expected_revision"}, {"selections", "expected_revision"})
                selections = data.get("selections")
                expected_revision = data.get("expected_revision")
                if not isinstance(selections, list) or isinstance(expected_revision, bool) or not isinstance(expected_revision, int):
                    raise ApiError(422, "invalid classification draft")
                return self._json(200, service.save_classification_draft(selections, expected_revision))
            if path == "/api/classifications/commit":
                _fields(data, {"preview_id"}, {"preview_id"})
                response, replay = self._idempotency(data, lambda: service.classification_commit(_string(data, "preview_id", maximum=64, required=True)))
                return self._json(200 if replay else 201, response)
            if path == "/api/classifications/retry":
                _fields(data, {"article_id"}, {"article_id"})
                response, replay = self._idempotency(data, lambda: service.retry_classification(_string(data, "article_id", maximum=64, required=True)))
                return self._json(200 if replay else 202, response)
            if path == "/api/categories/preview":
                _fields(data, {"action", "category_id", "target_category_id", "name", "description", "sort_order"}, {"action"})
                action = _string(data, "action", maximum=20, required=True)
                sort_order = data.get("sort_order")
                if sort_order is not None and (isinstance(sort_order, bool) or not isinstance(sort_order, int)):
                    raise ApiError(422, "sort_order must be an integer")
                return self._json(200, service.category_preview(
                    action,
                    category_id=_string(data, "category_id", maximum=64),
                    target_category_id=_string(data, "target_category_id", maximum=64),
                    name=_string(data, "name", maximum=80),
                    description=_string(data, "description", maximum=500),
                    sort_order=sort_order,
                ))
            if path == "/api/categories/commit":
                _fields(data, {"preview_id"}, {"preview_id"})
                response, replay = self._idempotency(data, lambda: service.category_commit(_string(data, "preview_id", maximum=64, required=True)))
                return self._json(200 if replay else 201, response)
            if path == "/api/reconciliation/scan":
                _fields(data, set())
                response, replay = self._idempotency(data, service.scan_reconciliation)
                return self._json(200 if replay else 201, response)
            if path == "/api/reconciliation/preview":
                _fields(data, {"reconciliation_id", "decision"}, {"reconciliation_id", "decision"})
                return self._json(200, service.reconciliation_preview(
                    _string(data, "reconciliation_id", maximum=64, required=True),
                    _string(data, "decision", maximum=20, required=True),
                ))
            if path == "/api/reconciliation/commit":
                _fields(data, {"preview_id"}, {"preview_id"})
                response, replay = self._idempotency(data, lambda: service.reconciliation_commit(_string(data, "preview_id", maximum=64, required=True)))
                return self._json(200 if replay else 201, response)
            if path == "/api/governance":
                _fields(data, set())
                response, replay = self._idempotency(data, service.enqueue_governance)
                return self._json(200, {**response, "replay": replay})
            if path == "/api/article/save":
                _fields(data, {"path", "markdown", "revision", "force"}, {"path", "markdown", "revision"})
                force = data.get("force", False)
                if not isinstance(force, bool):
                    raise ApiError(422, "force must be a boolean")
                response, _ = self._idempotency(data, lambda: service.save_article(
                    _string(data, "path", maximum=512, required=True),
                    _string(data, "markdown", maximum=None, required=True),
                    _string(data, "revision", maximum=128, required=True),
                    force=force,
                ))
                self.diagnostics.refresh()
                return self._json(409 if response.get("conflict") else 200, response)
            if path == "/api/ingest/commit":
                _fields(data, {"path", "disposition", "title", "category", "target_path"}, {"path", "disposition", "title"})
                response, _ = self._idempotency(data, lambda: service.ingest_commit(
                    _string(data, "path", maximum=512, required=True),
                    _string(data, "disposition", maximum=20, required=True),
                    title=_string(data, "title", maximum=120, required=True),
                    category=_string(data, "category", maximum=80),
                    target_path=_string(data, "target_path", maximum=512) or None,
                ))
                self.diagnostics.refresh()
                return self._json(200, response)
            if path == "/api/ingest/upload":
                _fields(data, {"filename", "content", "content_base64"}, {"filename"})
                has_text = "content" in data
                has_binary = "content_base64" in data
                if has_text == has_binary:
                    raise ApiError(422, "provide exactly one of content or content_base64")
                if has_binary:
                    encoded = _string(data, "content_base64", maximum=((MAX_INPUT_BYTES + 2) // 3) * 4, required=True)
                    try:
                        content = base64.b64decode(encoded, validate=True)
                    except (binascii.Error, ValueError) as exc:
                        raise ApiError(422, "content_base64 is invalid") from exc
                    if len(content) > MAX_INPUT_BYTES:
                        raise ApiError(413, "Raw file exceeds 10 MiB")
                else:
                    content = _string(data, "content", maximum=MAX_INPUT_BYTES, required=True)
                response, replay = self._idempotency(data, lambda: service.upload_raw(
                    _string(data, "filename", maximum=255, required=True),
                    content,
                ))
                return self._json(200 if replay or not response["created"] else 201, response)
            if path == "/api/merge/commit":
                _fields(data, {"source", "target"}, {"source", "target"})
                response, _ = self._idempotency(data, lambda: service.merge_commit(
                    _string(data, "source", maximum=512, required=True),
                    _string(data, "target", maximum=512, required=True),
                ))
                self.diagnostics.refresh()
                return self._json(200, response)
            match = re.fullmatch(r"/api/tasks/([a-f0-9]+)/(?P<action>cancel|retry)", path)
            if match:
                _fields(data, set())
                task_id, action = match.group(1), match.group("action")
                response, _ = self._idempotency(data, lambda: service.cancel_task(task_id) if action == "cancel" else service.retry_task(task_id))
                service._wake.set()
                return self._json(200, response)
            match = re.fullmatch(r"/api/operations/([A-Za-z0-9-]+)/rollback", path)
            if match:
                _fields(data, set())
                response, _ = self._idempotency(data, lambda: service.rollback(match.group(1)))
                self.diagnostics.refresh()
                return self._json(200, response)
            if path == "/api/share-previews":
                _fields(data, {"article_path", "source_revision", "attribution"}, {"article_path", "source_revision", "attribution"})
                article_path = _string(data, "article_path", maximum=512, required=True)
                source_revision = _string(data, "source_revision", maximum=128, required=True)
                attribution = _string(data, "attribution", maximum=80, required=True)
                article = service.read_article(article_path)
                if article["revision"] != source_revision:
                    raise ApiError(409, "article changed; reload before sharing")
                publication = app.platform.article_publication(self.context, article)
                if publication["state"] == "published":
                    raise ApiError(409, "article is already published and unchanged")
                if publication["state"] == "removed":
                    raise ApiError(409, "modify the private article before requesting relisting")
                if publication["state"] in {"submitted", "update_pending", "relist_pending"}:
                    raise ApiError(409, "article already has a submission in review")
                if attribution not in {"nickname", "anonymous"}:
                    raise ApiError(422, "invalid attribution")
                projected_markdown = public_markdown(article["markdown"])
                snapshot = {
                    "title": article["title"], "category": article["category"], "content_status": article["content_status"],
                    "markdown": projected_markdown, "summary": article_summary(projected_markdown),
                    "attribution": self.context.nickname if attribution == "nickname" else "匿名用户",
                    "source_summaries": [str(value)[:240] for value in article.get("sources", [])],
                }
                fingerprint = snapshot_fingerprint(snapshot)
                response, _ = self._idempotency(data, lambda: app.platform.create_preview(
                    self.context, article_path, source_revision, article["article_id"], fingerprint, snapshot,
                ))
                return self._json(201, response)
            if path == "/api/submissions":
                _fields(data, {"preview_id"}, {"preview_id"})
                response, replay = self._idempotency(data, lambda: app.platform.submit_preview(self.context, _string(data, "preview_id", maximum=64, required=True)))
                if app.review_worker is not None:
                    app.review_worker.wake()
                return self._json(200 if replay else 201, response)
            match = re.fullmatch(r"/api/submissions/([a-f0-9]{32})/(?P<action>ai-retry|withdraw)", path)
            if match:
                _fields(data, set())
                action = match.group("action")
                response, _ = self._idempotency(data, lambda: app.platform.retry_ai(self.context, match.group(1)) if action == "ai-retry" else app.platform.withdraw(self.context, match.group(1)))
                return self._json(200, response)
            match = re.fullmatch(r"/api/notifications/([a-f0-9]{32})/read", path)
            if match:
                _fields(data, set())
                response, _ = self._idempotency(data, lambda: app.platform.read_notification(self.context, match.group(1)))
                return self._json(200, response)
            match = re.fullmatch(r"/api/admin/submissions/([a-f0-9]{32})/decision", path)
            if match:
                self._require_role("admin")
                _fields(data, {"decision", "reason"}, {"decision", "reason"})
                response, _ = self._idempotency(data, lambda: app.platform.admin_decide(
                    self.context, match.group(1), _string(data, "decision", maximum=40, required=True), _string(data, "reason", maximum=1000, required=True),
                ))
                return self._json(200, response)
            match = re.fullmatch(r"/api/admin/public-entries/([a-f0-9]{32})/remove", path)
            if match:
                self._require_role("admin")
                _fields(data, {"reason"}, {"reason"})
                response, _ = self._idempotency(data, lambda: app.platform.remove_public(self.context, match.group(1), _string(data, "reason", maximum=1000, required=True)))
                return self._json(200, response)
            match = re.fullmatch(r"/api/admin/public-entries/([a-f0-9]{32})/relist", path)
            if match:
                self._require_role("admin")
                _fields(data, {"reason"}, {"reason"})
                response, _ = self._idempotency(data, lambda: app.platform.relist_public(
                    self.context, match.group(1), _string(data, "reason", maximum=1000, required=True),
                ))
                return self._json(200, response)
            match = re.fullmatch(r"/api/admin/reports/([a-f0-9]{32})/decision", path)
            if match:
                self._require_role("admin")
                _fields(data, {"action", "reason"}, {"action", "reason"})
                response, _ = self._idempotency(data, lambda: app.platform.decide_report(
                    self.context, match.group(1), _string(data, "action", maximum=20, required=True), _string(data, "reason", maximum=1000, required=True),
                ))
                return self._json(200, response)
            raise ApiError(404, "not found")

        def do_GET(self) -> None:
            self._begin_request()
            try:
                path = urlparse(self.path).path
                if path in {"/healthz", "/readyz"}:
                    return self._dispatch_get()
                self._validate_host()
                public = path in {"/api/platform/config", "/api/auth/session"} or path.startswith("/api/public/") or not path.startswith("/api/")
                self._prepare_context(required=app.multi_user and not public)
                self._dispatch_get()
            except ApiError as exc:
                self._error(exc)
            except FileNotFoundError:
                self._json(404, {"error": "not found"})
            except (ValueError, RuntimeError) as exc:
                self._json(400, {"error": str(exc)})
            except PermissionError:
                self._json(403, {"error": "insufficient role"})
            except Exception:
                operation_id = uuid.uuid4().hex
                print(f"Unhandled GET error operation={operation_id}", file=sys.stderr)
                self._json(500, {"error": "internal error", "operation_id": operation_id})
            finally:
                if self.service is not None:
                    self.service.set_request_actor(None)
                self._release_workspace_action()
                self._finish_request()

        def do_POST(self) -> None:
            self._begin_request()
            try:
                self._validate_host()
                self._validate_origin()
                path = urlparse(self.path).path
                public = path in {"/api/auth/register", "/api/auth/login", "/api/auth/recover"} or path.startswith("/api/public/entries/")
                self._prepare_context(required=app.multi_user and not public)
                if app.multi_user and not public:
                    self._require_csrf()
                if self.service is not None:
                    self.service.set_request_actor(self.context.user_id if self.context else None)
                self._dispatch_post()
            except ApiError as exc:
                self._error(exc)
            except FileNotFoundError:
                self._json(404, {"error": "not found"})
            except ValueError as exc:
                status = 409 if "idempotency" in str(exc) else 422
                self._json(status, {"error": str(exc)})
            except RuntimeError as exc:
                self._json(409, {"error": str(exc)})
            except PermissionError:
                self._json(403, {"error": "insufficient role"})
            except Exception:
                operation_id = uuid.uuid4().hex
                print(f"Unhandled POST error operation={operation_id}", file=sys.stderr)
                traceback.print_exc()
                self._json(500, {"error": "internal error", "operation_id": operation_id})
            finally:
                if self.service is not None:
                    self.service.set_request_actor(None)
                self._release_workspace_action()
                self._finish_request()

        def do_OPTIONS(self) -> None:
            self._begin_request()
            try:
                self._json(405, {"error": "method not allowed"})
            finally:
                self._finish_request()

    return Handler


class BoundedThreadingHTTPServer(ThreadingHTTPServer):
    def __init__(
        self, server_address, handler, *, max_concurrent: int, request_timeout: int,
        strict_transport_security: bool = False,
    ):
        self._capacity = threading.BoundedSemaphore(max_concurrent)
        self._request_timeout = request_timeout
        self._strict_transport_security = strict_transport_security
        super().__init__(server_address, handler)

    def get_request(self):
        request, address = super().get_request()
        request.settimeout(self._request_timeout)
        return request, address

    def process_request(self, request, client_address) -> None:
        if not self._capacity.acquire(blocking=False):
            request_id = uuid.uuid4().hex
            try:
                headers = (
                    b"HTTP/1.1 503 Service Unavailable\r\n"
                    b"Content-Type: application/json\r\n"
                    b"Content-Length: 33\r\n"
                    b"Retry-After: 1\r\n"
                    + f"X-Request-ID: {request_id}\r\n".encode("ascii")
                    + (b"Strict-Transport-Security: max-age=31536000\r\n" if self._strict_transport_security else b"")
                    + b"Connection: close\r\n\r\n"
                )
                request.sendall(headers + b'{"error":"server is at capacity"}')
                print(json.dumps({
                    "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                    "request_id": request_id,
                    "client_ip": client_address[0],
                    "event": "capacity_rejected",
                    "status": 503,
                }, ensure_ascii=True), flush=True)
            finally:
                self.shutdown_request(request)
            return
        super().process_request(request, client_address)

    def process_request_thread(self, request, client_address) -> None:
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._capacity.release()


def create_server(app: WikiApp, host: str = HOST, port: int = PORT) -> ThreadingHTTPServer:
    if host not in {"127.0.0.1", "localhost", "::1"}:
        raise ValueError("multi-user service remains loopback-only until the P0 deployment gate is verified")
    server = BoundedThreadingHTTPServer(
        (host, port), make_handler(app),
        max_concurrent=app.deployment.max_concurrent_requests,
        request_timeout=app.deployment.request_timeout_seconds,
        strict_transport_security=app.deployment.lan_mode,
    )
    server.daemon_threads = False
    server.block_on_close = True
    return server


def main() -> None:
    load_dotenv(ROOT)
    dev_origins = {value.strip() for value in env("WIKI_DEV_ORIGINS").split(",") if value.strip()}
    remote_worker_enabled = env("WIKI_DISABLE_REMOTE_WORKER").lower() not in {"1", "true", "yes"}
    configured_kinds = {value.strip() for value in env("WIKI_REMOTE_TASK_KINDS").split(",") if value.strip()}
    try:
        deployment = config_from_environment(env)
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc
    instance_lock = InstanceLock(instance_lock_path(ROOT))
    try:
        instance_lock.acquire()
    except RuntimeError as exc:
        raise SystemExit(str(exc)) from exc
    app = None
    try:
        if restore_in_progress(ROOT):
            raise SystemExit("an unfinished restore exists; rerun backup_restore.py restore before starting")
        app = create_app(
            load_environment=True,
            dev_origins=dev_origins,
            start_worker=remote_worker_enabled,
            remote_task_kinds=configured_kinds or None,
            multi_user=True,
            deployment_config=deployment,
        )
        try:
            port = int(env("WIKI_PORT", str(PORT)))
        except ValueError:
            raise SystemExit("WIKI_PORT must be an integer")
        server = create_server(app, port=port)
        signal.signal(
            signal.SIGTERM,
            lambda _signum, _frame: threading.Thread(target=server.shutdown, daemon=True).start(),
        )
        print(f"Wiki backend: http://{HOST}:{port}")
        if deployment.public_origin:
            print(f"Wiki public origin: {deployment.public_origin}")
        print("LLM: configured independently per private workspace")
        if not remote_worker_enabled:
            print("Remote worker: disabled; queued tasks will remain local")
        elif configured_kinds:
            print("Remote worker task kinds: " + ", ".join(sorted(configured_kinds)))
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
        finally:
            server.server_close()
    finally:
        if app is not None:
            app.close()
        instance_lock.release()


if __name__ == "__main__":
    main()
