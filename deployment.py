"""Validated deployment boundary for local and reverse-proxied operation."""

from __future__ import annotations

import ipaddress
from dataclasses import dataclass
from urllib.parse import urlsplit


REGISTRATION_MODES = {"open", "bootstrap", "invite", "closed"}


@dataclass(frozen=True)
class DeploymentConfig:
    public_origin: str | None = None
    trusted_proxy_cidrs: tuple[str, ...] = ()
    registration_mode: str = "open"
    max_concurrent_requests: int = 64
    request_timeout_seconds: int = 30
    min_free_bytes: int = 512 * 1024 * 1024

    def __post_init__(self) -> None:
        if self.registration_mode not in REGISTRATION_MODES:
            raise ValueError("WIKI_REGISTRATION_MODE must be open, bootstrap, invite, or closed")
        if self.max_concurrent_requests < 1 or self.max_concurrent_requests > 4096:
            raise ValueError("WIKI_MAX_CONCURRENT_REQUESTS must be between 1 and 4096")
        if self.request_timeout_seconds < 1 or self.request_timeout_seconds > 3600:
            raise ValueError("WIKI_REQUEST_TIMEOUT_SECONDS must be between 1 and 3600")
        if self.min_free_bytes < 0:
            raise ValueError("WIKI_MIN_FREE_BYTES must be non-negative")
        for value in self.trusted_proxy_cidrs:
            ipaddress.ip_network(value, strict=False)
        if self.public_origin is None:
            return
        parsed = urlsplit(self.public_origin)
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.path not in {"", "/"}
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("WIKI_PUBLIC_ORIGIN must be an HTTPS origin without path, query, or credentials")
        if parsed.port not in {None, 443}:
            raise ValueError("WIKI_PUBLIC_ORIGIN must use the default HTTPS port 443")
        canonical = f"https://{parsed.hostname.lower()}"
        if self.public_origin.rstrip("/").lower() != canonical:
            raise ValueError(f"WIKI_PUBLIC_ORIGIN must use canonical form: {canonical}")
        if not self.trusted_proxy_cidrs:
            raise ValueError("WIKI_TRUSTED_PROXY_CIDRS is required with WIKI_PUBLIC_ORIGIN")
        if self.registration_mode == "open":
            raise ValueError("LAN deployment requires bootstrap, invite, or closed registration")

    @property
    def lan_mode(self) -> bool:
        return self.public_origin is not None

    @property
    def public_authority(self) -> str | None:
        if self.public_origin is None:
            return None
        return urlsplit(self.public_origin).netloc.lower()

    @property
    def cookie_name(self) -> str:
        return "__Host-wiki_session" if self.lan_mode else "wiki_session"

    @property
    def trusted_proxy_networks(self):
        return tuple(ipaddress.ip_network(value, strict=False) for value in self.trusted_proxy_cidrs)

    def peer_is_trusted(self, peer: str) -> bool:
        try:
            address = ipaddress.ip_address(peer)
        except ValueError:
            return False
        return any(address in network for network in self.trusted_proxy_networks)

    def client_ip(self, peer: str, forwarded_for: str | None) -> str:
        peer_address = ipaddress.ip_address(peer)
        if not self.peer_is_trusted(peer) or not forwarded_for:
            return str(peer_address)
        values = [value.strip() for value in forwarded_for.split(",")]
        if not values or len(values) > 20 or any(not value for value in values):
            raise ValueError("invalid X-Forwarded-For header")
        chain = [ipaddress.ip_address(value) for value in values]
        chain.append(peer_address)
        for address in reversed(chain):
            if not any(address in network for network in self.trusted_proxy_networks):
                return str(address)
        return str(chain[0])

    def validate_forwarded_proto(self, peer: str, value: str | None) -> None:
        if not self.lan_mode:
            return
        if not self.peer_is_trusted(peer) or value != "https":
            raise ValueError("trusted reverse proxy must provide X-Forwarded-Proto: https")

    def trusted_host(self, host: str, backend_port: int) -> bool:
        if self.lan_mode:
            return host.lower() == self.public_authority
        return host in {f"127.0.0.1:{backend_port}", f"localhost:{backend_port}"}

    def trusted_origin(self, origin: str | None, backend_port: int, dev_origins: set[str]) -> bool:
        if self.lan_mode:
            return origin == self.public_origin
        allowed = {f"http://127.0.0.1:{backend_port}", f"http://localhost:{backend_port}", *dev_origins}
        return origin in allowed


def config_from_environment(getenv) -> DeploymentConfig:
    def integer(name: str, default: int) -> int:
        raw = getenv(name, str(default))
        try:
            return int(raw)
        except ValueError as exc:
            raise ValueError(f"{name} must be an integer") from exc

    public_origin = getenv("WIKI_PUBLIC_ORIGIN", "").rstrip("/") or None
    proxies = tuple(value.strip() for value in getenv("WIKI_TRUSTED_PROXY_CIDRS", "").split(",") if value.strip())
    default_registration = "bootstrap" if public_origin else "open"
    return DeploymentConfig(
        public_origin=public_origin,
        trusted_proxy_cidrs=proxies,
        registration_mode=getenv("WIKI_REGISTRATION_MODE", default_registration),
        max_concurrent_requests=integer("WIKI_MAX_CONCURRENT_REQUESTS", 64),
        request_timeout_seconds=integer("WIKI_REQUEST_TIMEOUT_SECONDS", 30),
        min_free_bytes=integer("WIKI_MIN_FREE_BYTES", 512 * 1024 * 1024),
    )
