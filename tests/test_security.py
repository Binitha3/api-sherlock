from __future__ import annotations

import pytest

from app.core import security
from app.core.security import BlockedTargetError, DnsLookupError


@pytest.mark.parametrize(
    ("address", "reason"),
    [
        ("127.0.0.1", "loopback"),
        ("10.2.3.4", "private"),
        ("172.16.1.2", "private"),
        ("192.168.1.4", "private"),
        ("169.254.169.254", "metadata"),
        ("169.254.1.2", "link-local"),
        ("::1", "loopback"),
        ("fc00::1", "private"),
    ],
)
def test_forbidden_ip_ranges(address: str, reason: str) -> None:
    assert reason in (security.forbidden_reason(address) or "")


def test_safe_public_host_is_allowed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(security, "resolve_target_ips", lambda host, port: ["93.184.216.34"])
    security.assert_safe_url("https://api.example.test/v1")


def test_mixed_public_and_private_dns_is_blocked(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        security,
        "resolve_target_ips",
        lambda host, port: ["93.184.216.34", "10.0.0.8"],
    )
    with pytest.raises(BlockedTargetError, match="private network"):
        security.assert_safe_url("https://api.example.test")


def test_private_address_can_only_be_allowed_by_explicit_development_setting() -> None:
    security.assert_safe_url("http://127.0.0.1:8001", allow_private_hosts=True)


def test_non_http_scheme_is_blocked() -> None:
    with pytest.raises(BlockedTargetError, match="http or https"):
        security.assert_safe_url("file:///etc/passwd")


def test_dns_failure_is_reported(monkeypatch: pytest.MonkeyPatch) -> None:
    def fail(host: str, port: int) -> list[str]:
        raise DnsLookupError(f"Could not resolve host '{host}'.")

    monkeypatch.setattr(security, "resolve_target_ips", fail)
    with pytest.raises(DnsLookupError):
        security.assert_safe_url("https://missing.invalid")
