from __future__ import annotations

import socket

import pytest

import security
from security import RemoteError, resolve_relative_file, resolve_public, validate_llm_endpoint, validate_public_url


@pytest.mark.parametrize("url", [
    "file:///etc/passwd", "http://user:pass@example.com/", "http://localhost/",
    "http://127.0.0.1/", "https://example.com:444/", "http://singlelabel/",
])
def test_public_url_rejects_unsafe_shapes(url: str):
    with pytest.raises(RemoteError):
        canonical, host, port, _ = validate_public_url(url)
        resolve_public(host, port)


def test_mixed_public_private_dns_is_rejected(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(socket, "getaddrinfo", lambda *args, **kwargs: [
        (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443)),
        (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 443)),
    ])
    with pytest.raises(RemoteError, match="non-public"):
        resolve_public("example.com", 443)


def test_fake_ip_requires_explicit_hostname_exception(monkeypatch: pytest.MonkeyPatch):
    fake = (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("198.18.0.10", 443))
    monkeypatch.setattr(socket, "getaddrinfo", lambda *args, **kwargs: [fake])
    with pytest.raises(RemoteError, match="non-public"):
        resolve_public("example.com", 443)
    assert resolve_public("example.com", 443, allow_fake_ip=True) == [fake]
    with pytest.raises(RemoteError, match="non-public"):
        resolve_public("198.18.0.10", 443, allow_fake_ip=True)


def test_fake_ip_exception_does_not_allow_other_private_results(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(socket, "getaddrinfo", lambda *args, **kwargs: [
        (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("198.18.0.10", 443)),
        (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 443)),
    ])
    with pytest.raises(RemoteError, match="non-public"):
        resolve_public("example.com", 443, allow_fake_ip=True)


def test_llm_endpoint_loopback_exception(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(socket, "getaddrinfo", lambda *args, **kwargs: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 11434))])
    validate_llm_endpoint("http://127.0.0.1:11434/v1")


def test_public_http_llm_requires_explicit_exception():
    with pytest.raises(ValueError, match="LLM_ALLOW_INSECURE_HTTP"):
        validate_llm_endpoint("http://93.184.216.34:8080/v1")
    validate_llm_endpoint(
        "http://93.184.216.34:8080/v1",
        allow_insecure_http=True,
    )


def test_path_helper_rejects_symlink_escape(kb_root, tmp_path):
    outside = tmp_path.parent / "outside.md"
    outside.write_text("secret", encoding="utf-8")
    link = kb_root / "wiki" / "concepts" / "escape.md"
    link.symlink_to(outside)
    with pytest.raises(ValueError):
        resolve_relative_file(kb_root / "wiki", "concepts/escape.md", suffixes={".md"})
