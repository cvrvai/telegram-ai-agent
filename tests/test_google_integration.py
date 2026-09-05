"""Google Calendar/Gmail OAuth storage, credential lifecycle, and account wiring.

These tests never reach Google's network: token exchange and refresh are
patched out, matching how test_business_services.py exercises the repository
contract against the SQLite adapter directly.
"""

from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from google.oauth2.credentials import Credentials

from app.integrations import google_auth
from app.integrations.google_account import GoogleAccount
from app.storage.business import BusinessRepository


class GoogleTokenStorageTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.repository = BusinessRepository(str(Path(self.temp_dir.name) / "business.db"))
        await self.repository.init()

    async def asyncTearDown(self) -> None:
        self.temp_dir.cleanup()

    async def test_google_token_round_trips_and_deletes(self) -> None:
        self.assertIsNone(await self.repository.get_google_token(42))
        await self.repository.save_google_token(42, '{"token":"abc"}', "calendar,gmail.send")
        stored = await self.repository.get_google_token(42)
        self.assertEqual(stored["token_json"], '{"token":"abc"}')
        self.assertEqual(stored["scopes"], "calendar,gmail.send")
        await self.repository.save_google_token(42, '{"token":"xyz"}', "calendar")
        self.assertEqual((await self.repository.get_google_token(42))["token_json"], '{"token":"xyz"}')
        await self.repository.delete_google_token(42)
        self.assertIsNone(await self.repository.get_google_token(42))

    async def test_oauth_state_is_single_use_and_scoped_to_its_owner(self) -> None:
        state = await self.repository.create_google_oauth_state(42)
        self.assertEqual(await self.repository.consume_google_oauth_state(state), 42)
        # Single-use: consuming again fails even though it was valid a moment ago.
        self.assertIsNone(await self.repository.consume_google_oauth_state(state))
        self.assertIsNone(await self.repository.consume_google_oauth_state("never-issued"))

    async def test_expired_oauth_state_is_rejected(self) -> None:
        state = await self.repository.create_google_oauth_state(42, ttl_minutes=-1)
        self.assertIsNone(await self.repository.consume_google_oauth_state(state))


class GoogleAuthHelperTests(unittest.TestCase):
    def test_credentials_round_trip_through_storage_record(self) -> None:
        original = Credentials(
            token="access-token",
            refresh_token="refresh-token",
            token_uri="https://oauth2.googleapis.com/token",
            client_id="client-id",
            client_secret="client-secret",
            scopes=[google_auth.CALENDAR_SCOPE],
        )
        record = google_auth.credentials_to_record(original)
        restored = google_auth.credentials_from_record(record)
        self.assertEqual(restored.token, "access-token")
        self.assertEqual(restored.refresh_token, "refresh-token")
        self.assertEqual(restored.scopes, [google_auth.CALENDAR_SCOPE])

    def test_build_auth_url_is_a_local_operation_and_carries_state(self) -> None:
        url = google_auth.build_auth_url("client-id", "client-secret", "https://example.com/oauth/google/callback", "the-state")
        self.assertIn("state=the-state", url)
        self.assertIn("accounts.google.com", url)

    def test_ensure_fresh_returns_false_for_a_valid_credential(self) -> None:
        creds = Credentials(
            token="access-token",
            refresh_token="refresh-token",
            expiry=datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(hours=1),
        )
        self.assertFalse(google_auth.ensure_fresh(creds))

    def test_ensure_fresh_refreshes_an_expired_credential(self) -> None:
        creds = Credentials(
            token="stale-token",
            refresh_token="refresh-token",
            expiry=datetime(2000, 1, 1),
        )
        with patch.object(Credentials, "refresh") as fake_refresh:
            self.assertTrue(google_auth.ensure_fresh(creds))
            fake_refresh.assert_called_once()

    def test_ensure_fresh_without_a_refresh_token_raises(self) -> None:
        creds = Credentials(token="stale-token", refresh_token=None, expiry=datetime(2000, 1, 1))
        with self.assertRaises(RuntimeError):
            google_auth.ensure_fresh(creds)


class GoogleAccountTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.repository = BusinessRepository(str(Path(self.temp_dir.name) / "business.db"))
        await self.repository.init()

    async def asyncTearDown(self) -> None:
        self.temp_dir.cleanup()

    async def test_unconfigured_account_refuses_to_build_a_connect_url(self) -> None:
        account = GoogleAccount(self.repository, None, None, None)
        self.assertFalse(account.configured())
        with self.assertRaises(RuntimeError):
            await account.connect_url(42)

    async def test_connect_url_binds_a_fresh_state_to_the_requesting_user(self) -> None:
        account = GoogleAccount(self.repository, "client-id", "client-secret", "https://example.com/oauth/google/callback")
        url = await account.connect_url(42)
        self.assertIn("accounts.google.com", url)
        # The state embedded in the URL is the one create_google_oauth_state
        # persisted -- pull it back out and confirm it resolves to user 42.
        state = url.split("state=")[1].split("&")[0]
        self.assertEqual(await self.repository.consume_google_oauth_state(state), 42)

    async def test_handle_callback_rejects_an_unknown_or_reused_state(self) -> None:
        account = GoogleAccount(self.repository, "client-id", "client-secret", "https://example.com/oauth/google/callback")
        with self.assertRaises(ValueError):
            await account.handle_callback("some-code", "unknown-state")

    async def test_handle_callback_stores_the_exchanged_token_for_the_state_owner(self) -> None:
        account = GoogleAccount(self.repository, "client-id", "client-secret", "https://example.com/oauth/google/callback")
        state = await self.repository.create_google_oauth_state(42)
        fake_credentials = Credentials(token="issued-token", refresh_token="issued-refresh", scopes=[google_auth.CALENDAR_SCOPE])
        with patch("app.integrations.google_account.google_auth.exchange_code", return_value=fake_credentials) as fake_exchange:
            user_id = await account.handle_callback("auth-code", state)
        fake_exchange.assert_called_once_with("client-id", "client-secret", "https://example.com/oauth/google/callback", "auth-code")
        self.assertEqual(user_id, 42)
        self.assertTrue(await account.is_connected(42))

    async def test_get_credentials_returns_none_when_never_connected(self) -> None:
        account = GoogleAccount(self.repository, "client-id", "client-secret", "https://example.com/oauth/google/callback")
        self.assertIsNone(await account.get_credentials(42))

    async def test_get_credentials_persists_a_refreshed_token(self) -> None:
        account = GoogleAccount(self.repository, "client-id", "client-secret", "https://example.com/oauth/google/callback")
        stale = Credentials(token="stale", refresh_token="refresh-token", expiry=datetime(2000, 1, 1), scopes=[google_auth.CALENDAR_SCOPE])
        await self.repository.save_google_token(42, google_auth.credentials_to_record(stale), google_auth.CALENDAR_SCOPE)

        def fake_refresh(self, request):
            self.token = "refreshed"
            self.expiry = datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(hours=1)

        with patch.object(Credentials, "refresh", fake_refresh):
            credentials = await account.get_credentials(42)
        self.assertEqual(credentials.token, "refreshed")
        # The refreshed token, not the stale one, is what a second read returns.
        stored = await self.repository.get_google_token(42)
        self.assertIn("refreshed", stored["token_json"])

    async def test_disconnect_removes_the_stored_token(self) -> None:
        account = GoogleAccount(self.repository, "client-id", "client-secret", "https://example.com/oauth/google/callback")
        await self.repository.save_google_token(42, '{"token":"abc"}', google_auth.CALENDAR_SCOPE)
        await account.disconnect(42)
        self.assertFalse(await account.is_connected(42))


if __name__ == "__main__":
    unittest.main()
