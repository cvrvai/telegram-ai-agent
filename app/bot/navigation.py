"""Compact callback route helpers for new Telegram screens."""

from __future__ import annotations


def route(*parts: object) -> bytes:
    """Encode a callback route while keeping the payload human-readable."""
    return ":".join(str(part) for part in parts).encode("utf-8")


def parse(data: bytes | str) -> list[str]:
    value = data.decode("utf-8") if isinstance(data, bytes) else str(data)
    return value.split(":")


def is_route(data: bytes | str, prefix: str) -> bool:
    return (data.decode("utf-8") if isinstance(data, bytes) else str(data)).startswith(prefix + ":")
