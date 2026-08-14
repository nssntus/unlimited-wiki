"""Persistent local model configuration with secret-safe public views."""

from __future__ import annotations

import json
import ipaddress
import os
from pathlib import Path
from urllib.parse import urlsplit

from security import validate_llm_endpoint
from wiki_service import LLMConfig

PROVIDERS = {"openai", "deepseek", "ollama", "openai-compatible"}


def settings_path(project_root: Path) -> Path:
    return project_root.resolve() / ".wiki-state" / "model-settings.json"


def build_config(provider: str, base_url: str, api_key: str, model: str, *, require_model: bool = True, allow_private: bool = True) -> LLMConfig:
    provider = provider.strip()
    base_url = base_url.strip().rstrip("/")
    api_key = api_key.strip()
    model = model.strip()
    if provider not in PROVIDERS:
        raise ValueError("invalid model provider")
    if not base_url or len(base_url) > 2048:
        raise ValueError("Base URL is required and must not exceed 2048 characters")
    if len(api_key) > 8192:
        raise ValueError("API key exceeds 8192 characters")
    if require_model and (not model or len(model) > 200):
        raise ValueError("model is required and must not exceed 200 characters")
    parsed = urlsplit(base_url)
    host = (parsed.hostname or "").lower().rstrip(".")
    if not allow_private:
        if host == "localhost" or host.endswith(".localhost") or host.endswith(".local"):
            raise ValueError("private model endpoints are not available in multi-user mode")
        try:
            address = ipaddress.ip_address(host)
        except ValueError:
            address = None
        if address is not None and not address.is_global:
            raise ValueError("private model endpoints are not available in multi-user mode")
    validate_llm_endpoint(base_url, allow_private=allow_private, allow_insecure_http=True)
    scheme = urlsplit(base_url).scheme
    return LLMConfig(
        provider=provider,
        base_url=base_url,
        api_key=api_key,
        model=model,
        allow_private=allow_private,
        allow_insecure_http=scheme == "http",
    )


def load_model_settings(project_root: Path) -> LLMConfig:
    path = settings_path(project_root)
    if not path.is_file():
        return LLMConfig()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError("model settings must be an object")
        return build_config(
            str(data.get("provider", "")),
            str(data.get("base_url", "")),
            str(data.get("api_key", "")),
            str(data.get("model", "")),
        )
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        print(f"Warning: local model settings were ignored ({type(exc).__name__})")
        return LLMConfig()


def save_model_settings(project_root: Path, config: LLMConfig) -> None:
    path = settings_path(project_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(path.parent, 0o700)
    payload = json.dumps(
        {
            "provider": config.provider,
            "base_url": config.base_url,
            "api_key": config.api_key,
            "model": config.model,
        },
        ensure_ascii=False,
        indent=2,
    ).encode("utf-8")
    temporary = path.with_suffix(".tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        os.chmod(path, 0o600)
    finally:
        if temporary.exists():
            temporary.unlink()


def public_model_settings(config: LLMConfig) -> dict:
    return {
        "configured": config.configured,
        "provider": config.provider or None,
        "base_url": config.base_url,
        "model": config.model or None,
        "has_api_key": bool(config.api_key),
        "insecure_http": config.allow_insecure_http,
    }
