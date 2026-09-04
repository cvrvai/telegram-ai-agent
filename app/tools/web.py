"""Bounded, read-only live web/API fetching for business questions."""

from __future__ import annotations

import ipaddress
from urllib.parse import urlparse

import httpx


def _safe_url(url: str) -> str:
    parsed = urlparse(url if "://" in url else f"https://{url}")
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("Use a public http(s) URL.")
    host = parsed.hostname.lower()
    if host in {"localhost", "localhost.localdomain"}:
        raise ValueError("Local URLs are not allowed.")
    try:
        address = ipaddress.ip_address(host)
        if address.is_private or address.is_loopback or address.is_link_local:
            raise ValueError("Private network URLs are not allowed.")
    except ValueError as exc:
        if str(exc).startswith("Private network"):
            raise
    return parsed.geturl()


async def fetch_live(url: str, max_chars: int = 12000) -> str:
    target = _safe_url(url)
    async with httpx.AsyncClient(timeout=12, follow_redirects=True, headers={"User-Agent": "TelegramBusinessAssistant/1.0"}) as client:
        response = await client.get(target)
        response.raise_for_status()
    content_type = response.headers.get("content-type", "")
    if not any(kind in content_type for kind in ("text/", "json", "xml", "javascript")):
        return f"Fetched {target} ({content_type or 'unknown content'}); binary content is not displayed."
    text = response.text
    return f"Source: {target}\nContent type: {content_type}\n\n{text[:max_chars]}"
