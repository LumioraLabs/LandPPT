"""Small, reusable HTTP security helpers."""

from __future__ import annotations

import ipaddress
from typing import Iterable, Optional
from urllib.parse import urlsplit

from ..core.config import app_config


def _split_csv(value: str | None) -> list[str]:
    return [part.strip() for part in str(value or "").split(",") if part.strip()]


def _parse_ip(value: object) -> Optional[ipaddress.IPv4Address | ipaddress.IPv6Address]:
    try:
        return ipaddress.ip_address(str(value or "").strip())
    except ValueError:
        return None


def _trusted_proxy_networks() -> Iterable[ipaddress.IPv4Network | ipaddress.IPv6Network]:
    for cidr in _split_csv(app_config.trusted_proxy_cidrs):
        try:
            yield ipaddress.ip_network(cidr, strict=False)
        except ValueError:
            continue


def is_trusted_proxy(remote_host: object) -> bool:
    """Return whether the direct peer is an explicitly configured proxy."""
    address = _parse_ip(remote_host)
    return bool(address and any(address in network for network in _trusted_proxy_networks()))


def get_client_ip(request) -> Optional[str]:
    """Resolve a client IP without trusting caller-controlled proxy headers."""
    client = getattr(request, "client", None)
    remote_host = getattr(client, "host", None)
    remote_address = _parse_ip(remote_host)

    if remote_address and is_trusted_proxy(remote_address):
        headers = request.headers
        candidates = [
            headers.get("cf-connecting-ip"),
            headers.get("x-real-ip"),
        ]
        xff = headers.get("x-forwarded-for")
        if xff:
            # The left-most value is the original client when trusted proxies
            # append rather than replace X-Forwarded-For.
            candidates.append(xff.split(",", 1)[0])
        for candidate in candidates:
            address = _parse_ip(candidate)
            if address:
                return str(address)

    return str(remote_address) if remote_address else None


def safe_local_redirect(value: object, default: str = "/dashboard") -> str:
    """Accept only a local absolute-path redirect target."""
    candidate = str(value or "").strip()
    if not candidate or not candidate.startswith("/") or candidate.startswith("//"):
        return default
    if any(character in candidate for character in ("\\", "\r", "\n", "\x00")):
        return default

    parsed = urlsplit(candidate)
    if parsed.scheme or parsed.netloc:
        return default
    return candidate


def normalized_origin(value: object) -> Optional[str]:
    """Normalize an HTTP(S) origin for exact comparison."""
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        parsed = urlsplit(raw)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            return None
        port = parsed.port
        default_port = 80 if parsed.scheme == "http" else 443
        host = parsed.hostname.lower()
        if ":" in host and not host.startswith("["):
            host = f"[{host}]"
        authority = host if port in {None, default_port} else f"{host}:{port}"
        return f"{parsed.scheme.lower()}://{authority}"
    except (TypeError, ValueError):
        return None
