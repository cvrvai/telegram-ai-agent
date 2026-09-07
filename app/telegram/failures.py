"""Say what actually failed when an approved draft cannot be carried out.

Every approval path shared one catch-all reply, "Telegram could not deliver
the message". That is wrong for four of the five action types and actively
misleading for the fifth: a Google Calendar TLS failure was reported to the
owner as a Telegram delivery problem, which sends them looking in the wrong
place. Name the system that failed, and where the owner can act, say how.
"""

from __future__ import annotations

import ssl

_SYSTEM_BY_ACTION = {
    "send_message": "Telegram",
    "email": "Gmail",
    "calendar": "Google Calendar",
    "crm": "the CRM integration",
    "task": "the task integration",
}


def _chain(exc: BaseException):
    """Libraries wrap transport errors, so the useful type is often nested."""
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        yield current
        current = current.__cause__ or current.__context__


def _cause(exc: BaseException) -> str | None:
    for error in _chain(exc):
        if isinstance(error, ssl.SSLError):
            return (
                "the TLS certificate for its server could not be verified, so the "
                "connection was refused. That is a network problem on the machine "
                "running me, not a problem with your request."
            )
        name = type(error).__name__
        if name == "RefreshError":
            return "the saved Google authorisation was rejected. Reconnect with /connectgoogle and try again."
        if name == "HttpError":
            status = getattr(getattr(error, "resp", None), "status", None)
            return f"it rejected the request (HTTP {status})." if status else "it rejected the request."
        if isinstance(error, (TimeoutError, ConnectionError)):
            return "it did not respond in time."
        if isinstance(error, OSError):
            return "it could not be reached over the network."
    return None


def describe_action_failure(action_type: str | None, exc: BaseException) -> str:
    """One owner-facing line naming the real failure, for an approved action."""
    system = _SYSTEM_BY_ACTION.get(action_type or "", "the integration")
    cause = _cause(exc)
    if cause:
        return f"⚠️ Approved, but {system} failed: {cause}"
    return f"⚠️ Approved, but {system} failed ({type(exc).__name__}). The details are in the logs."
