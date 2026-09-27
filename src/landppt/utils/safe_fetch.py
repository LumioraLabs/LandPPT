"""SSRF-resistant HTTP fetching for URLs supplied by users or models."""

from __future__ import annotations

import ipaddress
import socket
from typing import Any
from urllib.parse import urljoin, urlsplit

import aiohttp


class UnsafeURLError(ValueError):
    """Raised when an outbound URL could reach a non-public network."""


class ResponseTooLargeError(ValueError):
    """Raised when a remote response exceeds its configured byte budget."""


def _validate_address(value: str) -> None:
    try:
        address = ipaddress.ip_address(value)
    except ValueError as exc:
        raise UnsafeURLError("URL resolved to an invalid IP address") from exc
    if not address.is_global:
        raise UnsafeURLError("Private, loopback, link-local, and reserved addresses are not allowed")


def validate_public_http_url(url: object) -> str:
    """Validate URL syntax and any literal IP before a network request."""
    candidate = str(url or "").strip()
    if not candidate:
        raise UnsafeURLError("URL is required")

    try:
        parsed = urlsplit(candidate)
        port = parsed.port
    except ValueError as exc:
        raise UnsafeURLError("URL is malformed") from exc

    if parsed.scheme.lower() not in {"http", "https"}:
        raise UnsafeURLError("Only http and https URLs are supported")
    if not parsed.hostname:
        raise UnsafeURLError("URL hostname is required")
    if parsed.username is not None or parsed.password is not None:
        raise UnsafeURLError("Credentials in URLs are not allowed")
    if port is not None and not 1 <= port <= 65535:
        raise UnsafeURLError("URL port is invalid")

    hostname = parsed.hostname.rstrip(".").lower()
    if (
        hostname in {"localhost", "localhost.localdomain", "metadata.google.internal"}
        or hostname.endswith((".localhost", ".local", ".internal"))
    ):
        raise UnsafeURLError("Local and internal hostnames are not allowed")

    try:
        ipaddress.ip_address(hostname)
    except ValueError:
        pass
    else:
        _validate_address(hostname)
    return candidate


class PublicNetworkResolver(aiohttp.abc.AbstractResolver):
    """Resolve DNS at connection time and reject every non-public result."""

    def __init__(self) -> None:
        self._resolver = aiohttp.DefaultResolver()

    async def resolve(
        self,
        host: str,
        port: int = 0,
        family: socket.AddressFamily = socket.AF_UNSPEC,
    ) -> list[dict[str, Any]]:
        url_host = f"[{host}]" if ":" in host and not host.startswith("[") else host
        validate_public_http_url(f"http://{url_host}:{port or 80}")
        results = await self._resolver.resolve(host, port, family)
        if not results:
            raise UnsafeURLError("URL hostname did not resolve")
        for result in results:
            _validate_address(str(result["host"]))
        return results

    async def close(self) -> None:
        await self._resolver.close()


def safe_client_session(**kwargs: Any) -> aiohttp.ClientSession:
    """Build a session whose connector cannot reach non-public DNS answers."""
    if "connector" in kwargs:
        raise ValueError("A custom connector is not allowed for safe fetches")
    connector = aiohttp.TCPConnector(
        resolver=PublicNetworkResolver(),
        use_dns_cache=False,
    )
    return aiohttp.ClientSession(connector=connector, **kwargs)


async def safe_request(
    session: aiohttp.ClientSession,
    method: str,
    url: object,
    *,
    max_redirects: int = 5,
    **kwargs: Any,
) -> aiohttp.ClientResponse:
    """Issue a request while validating the initial URL and every redirect."""
    current_url = validate_public_http_url(url)
    kwargs.pop("allow_redirects", None)

    for redirect_count in range(max_redirects + 1):
        response = await session.request(
            method,
            current_url,
            allow_redirects=False,
            **kwargs,
        )
        if response.status not in {301, 302, 303, 307, 308}:
            return response

        location = response.headers.get("location")
        if not location:
            return response
        if redirect_count >= max_redirects:
            response.release()
            raise UnsafeURLError("Too many redirects")

        redirected_url = urljoin(str(response.url), location)
        validate_public_http_url(redirected_url)
        response.release()
        current_url = redirected_url

    raise UnsafeURLError("Too many redirects")  # pragma: no cover


async def read_response_limited(
    response: aiohttp.ClientResponse,
    max_bytes: int,
) -> bytes:
    """Read a decoded response stream with a strict byte ceiling."""
    limit = max(1, int(max_bytes))
    content_length = response.headers.get("content-length")
    if content_length:
        try:
            declared_length = int(content_length)
        except ValueError:
            declared_length = None
        if declared_length is not None and declared_length > limit:
            raise ResponseTooLargeError(f"Remote response exceeds {limit} bytes")

    body = bytearray()
    async for chunk in response.content.iter_chunked(64 * 1024):
        body.extend(chunk)
        if len(body) > limit:
            raise ResponseTooLargeError(f"Remote response exceeds {limit} bytes")
    return bytes(body)
