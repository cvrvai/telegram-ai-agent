"""A Google Calendar TLS failure was reported to the owner as "Telegram could
not deliver the message". Lock each action type to its own real cause."""

from __future__ import annotations

import ssl
import unittest

from app.telegram.failures import describe_action_failure


class HttpResponse:
    def __init__(self, status: int) -> None:
        self.status = status


class HttpError(Exception):
    def __init__(self, status: int) -> None:
        super().__init__(f"HTTP {status}")
        self.resp = HttpResponse(status)


class RefreshError(Exception):
    pass


class DescribeActionFailureTests(unittest.TestCase):
    def test_calendar_tls_failure_never_blames_telegram(self) -> None:
        message = describe_action_failure("calendar", ssl.SSLCertVerificationError("bad cert"))
        self.assertIn("Google Calendar", message)
        self.assertIn("TLS certificate", message)
        self.assertNotIn("Telegram", message)

    def test_wrapped_cause_is_found_through_the_chain(self) -> None:
        try:
            try:
                raise ssl.SSLCertVerificationError("bad cert")
            except ssl.SSLError as inner:
                raise RuntimeError("event creation failed") from inner
        except RuntimeError as outer:
            message = describe_action_failure("calendar", outer)
        self.assertIn("TLS certificate", message)

    def test_expired_google_authorisation_tells_the_owner_to_reconnect(self) -> None:
        message = describe_action_failure("email", RefreshError("invalid_grant"))
        self.assertIn("Gmail", message)
        self.assertIn("/connectgoogle", message)

    def test_api_rejection_reports_the_status(self) -> None:
        message = describe_action_failure("calendar", HttpError(403))
        self.assertIn("HTTP 403", message)

    def test_telegram_is_named_only_for_message_sends(self) -> None:
        message = describe_action_failure("send_message", ConnectionError("reset"))
        self.assertIn("Telegram", message)

    def test_unknown_cause_names_the_exception_type(self) -> None:
        message = describe_action_failure("crm", ValueError("boom"))
        self.assertIn("the CRM integration", message)
        self.assertIn("ValueError", message)

    def test_unknown_action_type_does_not_guess_a_system(self) -> None:
        message = describe_action_failure(None, ValueError("boom"))
        self.assertIn("the integration", message)
        self.assertNotIn("Telegram", message)


if __name__ == "__main__":
    unittest.main()
