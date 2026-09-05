"""Owner-scoped Google account connection: OAuth lifecycle and credential access.

Only the assistant owner may connect a Google account -- the Calendar and
inbox behind it are that one person's, not a shared workspace resource, so
this deliberately does not generalize to other approved users. Callers
(main.py) still enforce the owner+private-chat check on every command; this
class assumes that check already happened.
"""

from __future__ import annotations

from typing import Any, Optional

from . import google_auth


class GoogleAccount:
    def __init__(self, repository: Any, client_id: Optional[str], client_secret: Optional[str], redirect_uri: Optional[str]) -> None:
        self.repository = repository
        self.client_id = client_id
        self.client_secret = client_secret
        self.redirect_uri = redirect_uri

    def configured(self) -> bool:
        return bool(self.client_id and self.client_secret and self.redirect_uri)

    async def connect_url(self, user_id: int) -> str:
        if not self.configured():
            raise RuntimeError(
                "Google integration is not configured. Set GOOGLE_CLIENT_ID, "
                "GOOGLE_CLIENT_SECRET and GOOGLE_OAUTH_REDIRECT_URI."
            )
        state = await self.repository.create_google_oauth_state(user_id)
        return google_auth.build_auth_url(self.client_id, self.client_secret, self.redirect_uri, state)

    async def handle_callback(self, code: str, state: str) -> int:
        if not code:
            raise ValueError("Google did not return an authorization code.")
        user_id = await self.repository.consume_google_oauth_state(state)
        if user_id is None:
            raise ValueError("This connection link expired or was already used. Ask for a new /connectgoogle link.")
        credentials = google_auth.exchange_code(self.client_id, self.client_secret, self.redirect_uri, code)
        await self.repository.save_google_token(user_id, google_auth.credentials_to_record(credentials), ",".join(credentials.scopes or []))
        return user_id

    async def is_connected(self, user_id: int) -> bool:
        return await self.repository.get_google_token(user_id) is not None

    async def get_credentials(self, user_id: int):
        record = await self.repository.get_google_token(user_id)
        if not record:
            return None
        credentials = google_auth.credentials_from_record(record["token_json"])
        if google_auth.ensure_fresh(credentials):
            await self.repository.save_google_token(user_id, google_auth.credentials_to_record(credentials), record.get("scopes", ""))
        return credentials

    async def disconnect(self, user_id: int) -> None:
        await self.repository.delete_google_token(user_id)
