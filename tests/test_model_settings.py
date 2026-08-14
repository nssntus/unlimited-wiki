from __future__ import annotations

import json

from model_settings import load_model_settings, save_model_settings
from wiki_service import LLMConfig


def test_model_settings_are_not_loaded_from_environment(kb_root, monkeypatch):
    monkeypatch.setenv("LLM_BASE_URL", "http://legacy.example.com/v1")
    monkeypatch.setenv("LLM_API_KEY", "legacy-key")
    monkeypatch.setenv("LLM_MODEL", "legacy-model")
    assert load_model_settings(kb_root).configured is False


def test_saved_model_settings_round_trip(kb_root):
    config = LLMConfig(
        provider="ollama",
        base_url="http://127.0.0.1:11434/v1",
        api_key="",
        model="qwen3",
        allow_private=True,
        allow_insecure_http=True,
    )
    save_model_settings(kb_root, config)
    loaded = load_model_settings(kb_root)
    assert loaded.provider == "ollama"
    assert loaded.base_url == "http://127.0.0.1:11434/v1"
    assert loaded.model == "qwen3"
    assert json.loads((kb_root / ".wiki-state" / "model-settings.json").read_text(encoding="utf-8"))["api_key"] == ""
