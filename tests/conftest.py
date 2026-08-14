from __future__ import annotations

import hashlib
import threading
from pathlib import Path

import pytest


BASE_ARTICLE = """# Base

> Category: concepts
> Status: 词条
> Aliases: Foundation
> Sources: [Source](source.md)

## 它做什么

Base 是一个用于测试的本地概念，它提供明确释义并保持内容稳定。

## 怎么用

在隔离夹具中使用。

## 例子

这是一个例子。

## See Also
"""


@pytest.fixture
def kb_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    for key in (
        "LLM_BASE_URL", "LLM_API_KEY", "LLM_MODEL", "OPENAI_API_KEY",
        "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY",
    ):
        monkeypatch.delenv(key, raising=False)
    (tmp_path / "wiki" / "concepts").mkdir(parents=True)
    (tmp_path / "wiki" / "tools").mkdir()
    (tmp_path / "raw" / "local").mkdir(parents=True)
    (tmp_path / "viewer" / "dist").mkdir(parents=True)
    (tmp_path / "viewer" / "dist" / "index.html").write_text("<!doctype html><main id='root'>test</main>", encoding="utf-8")
    (tmp_path / "wiki" / "concepts" / "base.md").write_text(BASE_ARTICLE, encoding="utf-8")
    (tmp_path / "wiki" / "concepts" / "source.md").write_text(
        BASE_ARTICLE.replace("# Base", "# Source").replace("Foundation", "Origin"), encoding="utf-8"
    )
    (tmp_path / "wiki" / "index.md").write_text("# Knowledge Base Index\n", encoding="utf-8")
    (tmp_path / "wiki" / "log.md").write_text("# Wiki Log\n", encoding="utf-8")
    return tmp_path


def tree_snapshot(root: Path) -> dict[str, str]:
    return {
        path.relative_to(root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in root.rglob("*")
        if path.is_file() and ".wiki-state" not in path.parts
    }


@pytest.fixture
def snapshot():
    return tree_snapshot


@pytest.fixture
def live_server(kb_root: Path):
    from serve import create_app, create_server

    app = create_app(kb_root, kb_root / "viewer", start_worker=False)
    server = create_server(app, host="127.0.0.1", port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address
    try:
        yield app, f"http://{host}:{port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
        app.service.close()
