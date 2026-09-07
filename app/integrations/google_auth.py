"""Google OAuth 2.0 helpers: authorization URL, code exchange, credential refresh.

Tokens are persisted as a JSON credential record (see credentials_to_record),
not a live SDK object, since storage is a plain string column shared with the
rest of app/storage. Convert at the storage boundary with
credentials_from_record/credentials_to_record.
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Optional

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import Flow

CALENDAR_SCOPE = "https://www.googleapis.com/auth/calendar.events"
GMAIL_READONLY_SCOPE = "https://www.googleapis.com/auth/gmail.readonly"
GMAIL_SEND_SCOPE = "https://www.googleapis.com/auth/gmail.send"
SCOPES = [CALENDAR_SCOPE, GMAIL_READONLY_SCOPE, GMAIL_SEND_SCOPE]


def _flow(client_id: str, client_secret: str, redirect_uri: str) -> Flow:
    client_config = {
        "web": {
            "client_id": client_id,
            "client_secret": client_secret,
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
            "redirect_uris": [redirect_uri],
        }
    }
    return Flow.from_client_config(client_config, scopes=SCOPES, redirect_uri=redirect_uri)


def build_auth_url(client_id: str, client_secret: str, redirect_uri: str, state: str) -> tuple[str, Optional[str]]:
    """Returns (url, code_verifier). The library generates a PKCE verifier here
    and Google demands the SAME one back at token exchange, so the caller must
    persist it with the state -- a second Flow object would generate a new one
    and the exchange fails with "invalid_grant: Missing code verifier"."""
    flow = _flow(client_id, client_secret, redirect_uri)
    auth_url, _ = flow.authorization_url(
        access_type="offline",
        include_granted_scopes="true",
        prompt="consent",  # guarantees a refresh_token even on re-consent
        state=state,
    )
    return auth_url, getattr(flow, "code_verifier", None)


def exchange_code(client_id: str, client_secret: str, redirect_uri: str, code: str, code_verifier: Optional[str] = None) -> Credentials:
    flow = _flow(client_id, client_secret, redirect_uri)
    if code_verifier:
        flow.code_verifier = code_verifier
    flow.fetch_token(code=code)
    return flow.credentials


def credentials_to_record(credentials: Credentials) -> str:
    return json.dumps(
        {
            "token": credentials.token,
            "refresh_token": credentials.refresh_token,
            "token_uri": credentials.token_uri,
            "client_id": credentials.client_id,
            "client_secret": credentials.client_secret,
            "scopes": list(credentials.scopes or []),
            "expiry": credentials.expiry.isoformat() if credentials.expiry else None,
        }
    )


def credentials_from_record(record_json: str) -> Credentials:
    data = json.loads(record_json)
    # expiry round-trips as a naive UTC isoformat string; without it every
    # restored credential looks permanently valid and is never refreshed.
    expiry = datetime.fromisoformat(data["expiry"]) if data.get("expiry") else None
    return Credentials(
        token=data.get("token"),
        refresh_token=data.get("refresh_token"),
        token_uri=data.get("token_uri"),
        client_id=data.get("client_id"),
        client_secret=data.get("client_secret"),
        scopes=data.get("scopes"),
        expiry=expiry,
    )


def ensure_fresh(credentials: Credentials) -> bool:
    """Refresh in place if expired. Returns True if a refresh happened."""
    if credentials.valid:
        return False
    if not credentials.refresh_token:
        raise RuntimeError("Google connection has no refresh token; reconnect with /connectgoogle.")
    credentials.refresh(Request())
    return True
