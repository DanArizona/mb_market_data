"""Pushover placeholder is never a transport or credential reader."""
import os
from unittest import TestCase, mock

from mb_market_data.closeout_notifications import (
    CloseoutEvent, CloseoutNotice, PushoverStub, due_notice,
)


class CloseoutPushoverStubTests(TestCase):
    def setUp(self):
        self.notice=due_notice("2026-11-03","not ready")

    def test_disabled_by_default_and_never_claims_delivery(self):
        result=PushoverStub().dispatch(self.notice)
        self.assertEqual(result.provider,"pushover")
        self.assertEqual(result.outcome,"DISABLED")
        self.assertFalse(result.delivered)

    def test_explicit_enabled_request_still_does_not_send(self):
        result=PushoverStub(enabled=True).dispatch(self.notice)
        self.assertEqual(result.outcome,"NOT_IMPLEMENTED")
        self.assertFalse(result.delivered)

    def test_no_environment_token_or_credentials_are_used(self):
        with mock.patch.dict(os.environ,{
            "PUSHOVER_APP_TOKEN":"synthetic-sensitive-token",
            "PUSHOVER_USER_KEY":"synthetic-sensitive-key",
            "MB_PUSHOVER_ECFG":"C:\\nonexistent\\pushover.ecfg",
        }):
            with mock.patch("builtins.open",side_effect=AssertionError("unexpected secret read")):
                for flag in (False,True):
                    result=PushoverStub(enabled=flag).dispatch(self.notice)
                    self.assertNotIn("synthetic-sensitive",repr(result))
                    self.assertFalse(result.delivered)

    def test_due_notice_is_separate_from_credentials(self):
        self.assertEqual(self.notice.event,CloseoutEvent.PREPARATION_DUE)
        self.assertIn("2026-11-03",self.notice.message)
        self.assertEqual(self.notice.urgency,"warning")

    def test_recovery_event_can_share_future_channel(self):
        notice=CloseoutNotice(event=CloseoutEvent.DEADLINE_MISSED,
            source_session="2026-11-03",
            title="Missed closeout",
            message="Recovery required",
            urgency="critical")
        self.assertEqual(PushoverStub().dispatch(notice).outcome,"DISABLED")

    def test_invalid_urgency_rejected(self):
        with self.assertRaisesRegex(ValueError,"urgency"):
            CloseoutNotice(event=CloseoutEvent.PREPARATION_DUE,
                source_session="2026-11-03",title="test",message="test",
                urgency="siren")
