"""Outbound HTTP for URLs that come from users or from the model.

Blocks server-side request forgery: a URL whose host resolves to a loopback,
private, link-local (cloud metadata) or otherwise non-public address is
refused, and every redirect hop is checked again. Responses are size-capped.
"""
from __future__ import annotations

import ipaddress
import socket
from urllib.parse import urljoin, urlparse

import httpx

from .config import settings

MAX_BYTES = 40 * 1024 * 1024
USER_AGENT = "GrantForge/0.2 (+self-hosted grant application assistant)"


class UnsafeURL(ValueError):
    pass


def check_url(url: str) -> None:
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"}:
        raise UnsafeURL(f"Only http and https URLs are allowed: {url}")
    host = parsed.hostname
    if not host:
        raise UnsafeURL(f"URL has no host: {url}")
    if settings.allow_private_urls:
        return
    try:
        infos = socket.getaddrinfo(host, parsed.port or (443 if parsed.scheme == "https" else 80))
    except socket.gaierror as exc:
        raise UnsafeURL(f"Cannot resolve {host}: {exc}") from exc
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if not ip.is_global or ip.is_multicast:
            raise UnsafeURL(f"{host} resolves to a non-public address ({ip}); refused")


def safe_get(url: str, timeout: float = 30, max_redirects: int = 5, client: httpx.Client | None = None) -> httpx.Response:
    own = client is None
    client = client or httpx.Client(timeout=timeout, follow_redirects=False, headers={"User-Agent": USER_AGENT})
    try:
        current = url
        for _ in range(max_redirects + 1):
            check_url(current)
            with client.stream("GET", current) as r:
                if r.is_redirect and r.headers.get("location"):
                    current = urljoin(current, r.headers["location"])
                    continue
                r.raise_for_status()
                chunks, total = [], 0
                for chunk in r.iter_bytes():
                    total += len(chunk)
                    if total > MAX_BYTES:
                        raise ValueError(f"Response from {current} exceeds {MAX_BYTES // (1024 * 1024)} MB")
                    chunks.append(chunk)
                # iter_bytes() already decoded gzip/deflate, so drop the encoding headers.
                headers = {k: v for k, v in r.headers.items()
                           if k.lower() not in {"content-encoding", "content-length", "transfer-encoding"}}
                return httpx.Response(r.status_code, headers=headers, content=b"".join(chunks),
                                      request=httpx.Request("GET", current))
        raise ValueError(f"Too many redirects fetching {url}")
    finally:
        if own:
            client.close()
