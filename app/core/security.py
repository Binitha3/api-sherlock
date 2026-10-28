"""SSRF protection for user-supplied URLs.

Every target URL is checked before a connection is made: DNS is resolved first
and every resolved IP is rejected when it is loopback, link-local (including
the cloud metadata endpoint), private, or otherwise non-global. Redirects are
re-checked hop by hop by the runner. ALLOW_PRIVATE_HOSTS=true disables the
checks for local development only.
"""

from __future__ import annotations

import ipaddress
import socket
from urllib.parse import urlparse

from app.core.config import get_settings
from app.core.errors import AppError

__all__ = [
    "BlockedTargetError",
    "DnsLookupError",
    "assert_safe_url",
    "forbidden_reason",
    "resolve_target_ips",
]


class BlockedTargetError(AppError):
    """The URL points at an internal address or is otherwise not usable."""

    def __init__(self, message: str) -> None:
        super().__init__(message, code="blocked_target", status_code=422)


class DnsLookupError(AppError):
    """The host could not be resolved to any address."""

    def __init__(self, message: str) -> None:
        super().__init__(message, code="dns_failed", status_code=422)


def resolve_target_ips(host: str, port: int) -> list[str]:
    """Resolve a host to its IP addresses (isolated so tests can patch it)."""
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except (socket.gaierror, UnicodeError, OSError) as exc:
        raise DnsLookupError(f"Could not resolve host '{host}'.") from exc

    ips: list[str] = []
    seen: set[str] = set()
    for _family, _socktype, _proto, _canonname, sockaddr in infos:
        address = sockaddr[0]
        if address not in seen:
            seen.add(address)
            ips.append(address)
    if not ips:
        raise DnsLookupError(f"Could not resolve host '{host}'.")
    return ips


def forbidden_reason(ip_text: str) -> str | None:
    """Return why an IP must not be contacted, or None when it is public."""
    try:
        ip = ipaddress.ip_address(ip_text)
    except ValueError:
        return "not a valid IP address"
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    if str(ip) == "169.254.169.254":
        return "cloud metadata endpoint"
    if ip.is_loopback:
        return "loopback address"
    if ip.is_link_local:
        return "link-local address"
    if ip.is_private:
        return "private network address"
    if ip.is_multicast:
        return "multicast address"
    if ip.is_unspecified:
        return "unspecified address"
    if not ip.is_global:
        return "internal or reserved address"
    return None


def assert_safe_url(url: str, *, allow_private_hosts: bool | None = None) -> None:
    """Raise unless the URL is an http(s) URL whose host resolves to public IPs.

    `allow_private_hosts` defaults to the ALLOW_PRIVATE_HOSTS setting; pass it
    explicitly only where a caller has already made the trust decision.
    """
    settings = get_settings()
    allow = settings.allow_private_hosts if allow_private_hosts is None else allow_private_hosts

    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise BlockedTargetError(f"URL must use http or https, got '{parsed.scheme or 'none'}'.")
    host = parsed.hostname
    if not host:
        raise BlockedTargetError("URL has no host.")
    if allow:
        return

    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    for ip in resolve_target_ips(host, port):
        reason = forbidden_reason(ip)
        if reason:
            raise BlockedTargetError(
                f"Blocked request to '{host}' ({ip}): {reason}. "
                "Set ALLOW_PRIVATE_HOSTS=true only for local development."
            )
