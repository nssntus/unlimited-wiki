"""Input, path, and outbound-network security helpers."""

from __future__ import annotations

import http.client
import ipaddress
import socket
import ssl
from pathlib import Path, PurePosixPath
from urllib.parse import urljoin, urlsplit, urlunsplit


class RemoteError(RuntimeError):
    def __init__(self, code: str, message: str, *, retryable: bool = False):
        super().__init__(message)
        self.code = code
        self.retryable = retryable


FAKE_IP_NETWORK = ipaddress.ip_network("198.18.0.0/15")


def resolve_relative_file(
    root: Path,
    rel: str,
    *,
    suffixes: set[str],
    must_exist: bool = True,
) -> Path:
    if not isinstance(rel, str):
        raise ValueError("path must be a string")
    text = rel.strip()
    if not text or len(text) > 512 or "\\" in text or any(ord(ch) < 32 for ch in text):
        raise ValueError("invalid path")
    pure = PurePosixPath(text)
    if pure.is_absolute() or pure.as_posix() != text or any(part in {"", ".", ".."} for part in pure.parts):
        raise ValueError("invalid path")
    if pure.suffix.lower() not in suffixes:
        raise ValueError("unsupported file type")
    base = root.resolve()
    candidate = (base / pure).resolve(strict=must_exist)
    if not candidate.is_relative_to(base):
        raise ValueError("path escapes its root")
    if must_exist and (not candidate.is_file() or candidate.is_symlink()):
        raise FileNotFoundError(text)
    if not must_exist and candidate.parent.resolve().is_relative_to(base) is False:
        raise ValueError("path escapes its root")
    return candidate


def _normalized_ip(value: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address:
    ip = ipaddress.ip_address(value.split("%", 1)[0])
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
        return ip.ipv4_mapped
    return ip


def _is_public(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    if not ip.is_global:
        return False
    if isinstance(ip, ipaddress.IPv6Address):
        if ip.sixtofour and not ip.sixtofour.is_global:
            return False
        if ip.teredo and any(not part.is_global for part in ip.teredo):
            return False
    return True


def resolve_public(host: str, port: int, *, allow_fake_ip: bool = False) -> list[tuple]:
    try:
        addresses = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise RemoteError("dns_error", "The source hostname could not be resolved.", retryable=True) from exc
    if not addresses:
        raise RemoteError("dns_error", "The source hostname returned no addresses.", retryable=True)
    try:
        ipaddress.ip_address(host.split("%", 1)[0])
        host_is_ip_literal = True
    except ValueError:
        host_is_ip_literal = False
    for info in addresses:
        ip = _normalized_ip(info[4][0])
        fake_ip_allowed = allow_fake_ip and not host_is_ip_literal and ip in FAKE_IP_NETWORK
        if not _is_public(ip) and not fake_ip_allowed:
            raise RemoteError("blocked_address", "The source resolved to a non-public address.")
    return addresses


def validate_public_url(url: str) -> tuple[str, str, int, str]:
    if not isinstance(url, str) or not url or len(url) > 2048:
        raise RemoteError("invalid_url", "The source URL is invalid.")
    if "\\" in url or any(ord(ch) < 32 for ch in url):
        raise RemoteError("invalid_url", "The source URL contains forbidden characters.")
    parsed = urlsplit(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise RemoteError("invalid_url", "Only HTTP and HTTPS source URLs are allowed.")
    if parsed.username is not None or parsed.password is not None:
        raise RemoteError("invalid_url", "Source URLs cannot contain credentials.")
    if parsed.fragment:
        parsed = parsed._replace(fragment="")
    try:
        raw_host = parsed.hostname.split("%", 1)[0]
        try:
            host = str(ipaddress.ip_address(raw_host))
            host_is_ip = True
        except ValueError:
            host = raw_host.encode("idna").decode("ascii").lower().rstrip(".")
            host_is_ip = False
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
    except (UnicodeError, ValueError) as exc:
        raise RemoteError("invalid_url", "The source hostname or port is invalid.") from exc
    if host == "localhost" or host.endswith((".localhost", ".local")) or (not host_is_ip and "." not in host):
        raise RemoteError("blocked_address", "Local and single-label source hosts are blocked.")
    expected = 443 if parsed.scheme == "https" else 80
    if port != expected:
        raise RemoteError("blocked_port", "Web sources are restricted to ports 80 and 443.")
    netloc = f"[{host}]" if ":" in host else host
    canonical = urlunsplit((parsed.scheme, netloc, parsed.path or "/", parsed.query, ""))
    return canonical, host, port, parsed.scheme


class _PinnedHTTPConnection(http.client.HTTPConnection):
    def __init__(self, host: str, port: int, pinned_info: tuple, timeout: float):
        super().__init__(host, port=port, timeout=timeout)
        self._pinned_info = pinned_info

    def connect(self) -> None:
        family, socktype, proto, _canonname, sockaddr = self._pinned_info
        self.sock = socket.socket(family, socktype, proto)
        self.sock.settimeout(self.timeout)
        self.sock.connect(sockaddr)


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    def __init__(self, host: str, port: int, pinned_info: tuple, timeout: float):
        context = ssl.create_default_context()
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        super().__init__(host, port=port, timeout=timeout, context=context)
        self._pinned_info = pinned_info

    def connect(self) -> None:
        family, socktype, proto, _canonname, sockaddr = self._pinned_info
        raw = socket.socket(family, socktype, proto)
        raw.settimeout(self.timeout)
        raw.connect(sockaddr)
        self.sock = self._context.wrap_socket(raw, server_hostname=self.host)


class SafeFetcher:
    def __init__(self, *, timeout: float = 12, max_redirects: int = 5, allow_fake_ip: bool = False):
        self.timeout = timeout
        self.max_redirects = max_redirects
        self.allow_fake_ip = allow_fake_ip

    def fetch(
        self,
        url: str,
        *,
        method: str = "GET",
        body: bytes | None = None,
        headers: dict[str, str] | None = None,
        max_bytes: int = 2 * 1024 * 1024,
        content_types: tuple[str, ...] | None = None,
    ) -> tuple[bytes, str, dict[str, str]]:
        current = url
        request_method = method.upper()
        request_body = body
        for redirect_count in range(self.max_redirects + 1):
            canonical, host, port, scheme = validate_public_url(current)
            parsed = urlsplit(canonical)
            addresses = resolve_public(host, port, allow_fake_ip=self.allow_fake_ip)
            connection_cls = _PinnedHTTPSConnection if scheme == "https" else _PinnedHTTPConnection
            connection = connection_cls(host, port, addresses[0], self.timeout)
            request_headers = {
                "Host": host,
                "User-Agent": "LLMWiki/2.0 (+local knowledge workspace)",
                "Accept-Encoding": "identity",
                "Connection": "close",
            }
            request_headers.update(headers or {})
            target = parsed.path or "/"
            if parsed.query:
                target += "?" + parsed.query
            try:
                connection.request(request_method, target, body=request_body, headers=request_headers)
                response = connection.getresponse()
                if response.status in {301, 302, 303, 307, 308}:
                    location = response.getheader("Location")
                    response.read(4096)
                    if not location:
                        raise RemoteError("redirect_error", "The source returned an empty redirect.")
                    if redirect_count >= self.max_redirects:
                        raise RemoteError("redirect_error", "The source exceeded the redirect limit.")
                    next_url = urljoin(canonical, location)
                    next_scheme = urlsplit(next_url).scheme
                    if scheme == "https" and next_scheme == "http":
                        raise RemoteError("tls_downgrade", "HTTPS to HTTP redirects are blocked.")
                    if response.status == 303 or (response.status in {301, 302} and request_method == "POST"):
                        request_method, request_body = "GET", None
                    current = next_url
                    continue
                if response.status >= 400:
                    raise RemoteError("http_error", f"The source returned HTTP {response.status}.", retryable=response.status >= 500)
                declared = response.getheader("Content-Length")
                if declared:
                    try:
                        declared_size = int(declared)
                    except ValueError as exc:
                        raise RemoteError("invalid_response", "The source returned an invalid Content-Length.") from exc
                    if declared_size < 0 or declared_size > max_bytes:
                        raise RemoteError("response_too_large", "The source response exceeded the size limit.")
                media_type = (response.getheader("Content-Type") or "").split(";", 1)[0].strip().lower()
                if content_types and media_type and not any(media_type == item or media_type.startswith(item) for item in content_types):
                    raise RemoteError("unsupported_content", "The source returned an unsupported content type.")
                chunks: list[bytes] = []
                total = 0
                while True:
                    chunk = response.read(min(65536, max_bytes + 1 - total))
                    if not chunk:
                        break
                    chunks.append(chunk)
                    total += len(chunk)
                    if total > max_bytes:
                        raise RemoteError("response_too_large", "The source response exceeded the size limit.")
                response_headers = {key.lower(): value for key, value in response.getheaders()}
                return b"".join(chunks), canonical, response_headers
            except ssl.SSLError as exc:
                raise RemoteError("tls_error", "The source TLS certificate could not be verified.") from exc
            except (TimeoutError, socket.timeout) as exc:
                raise RemoteError("timeout", "The source request timed out.", retryable=True) from exc
            except OSError as exc:
                raise RemoteError("network_error", "The source request failed.", retryable=True) from exc
            finally:
                connection.close()
        raise RemoteError("redirect_error", "The source exceeded the redirect limit.")


def validate_llm_endpoint(
    url: str,
    *,
    allow_private: bool = False,
    allow_insecure_http: bool = False,
) -> None:
    parsed = urlsplit(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError("LLM_BASE_URL must be an HTTP(S) URL without credentials")
    try:
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
    except ValueError as exc:
        raise ValueError("LLM_BASE_URL port is invalid") from exc
    host = parsed.hostname.lower().rstrip(".")
    if host == "localhost" or host.endswith(".localhost"):
        return
    try:
        ip = _normalized_ip(host)
    except ValueError:
        ip = None
    if ip and ip.is_loopback:
        return
    if ip and _is_public(ip):
        if parsed.scheme == "http" and allow_insecure_http:
            return
        if parsed.scheme != "https" or port != 443:
            raise ValueError("public HTTP LLM endpoints require LLM_ALLOW_INSECURE_HTTP=1")
        return
    if ip and ip.is_private and allow_private:
        if parsed.scheme == "http" and not allow_insecure_http:
            raise ValueError("private HTTP LLM endpoints require LLM_ALLOW_INSECURE_HTTP=1")
        return
    if ip:
        raise ValueError("private LLM endpoints require LLM_ALLOW_PRIVATE=1")
    if host.endswith(".local"):
        if allow_private:
            if parsed.scheme == "http" and not allow_insecure_http:
                raise ValueError("private HTTP LLM endpoints require LLM_ALLOW_INSECURE_HTTP=1")
            return
        raise ValueError("private LLM endpoints require LLM_ALLOW_PRIVATE=1")
    if parsed.scheme == "http" and allow_insecure_http:
        return
    if parsed.scheme == "https" and port == 443:
        return
    raise ValueError("HTTP LLM endpoints require LLM_ALLOW_INSECURE_HTTP=1")
