"""Exchange-calendars contract: NYSE/Nasdaq holidays, early closes and DST.

Actual local package schedule is queried. No hardcoded holiday calendar.
"""
from datetime import date, datetime, time, timedelta, timezone
from unittest import TestCase

from mb_market_data.equity_closeout_calendar import (
    CALENDAR_CONTRACT, ET, equity_session, next_exchange_session,
    reminder_due_at, require_postclose_source_time,
    require_exchange_session, validate_next_exchange_session,
)


class EquityCloseoutCalendarTests(TestCase):
    def test_regular_session_close_1600_eastern(self):
        session=equity_session(date(2026,11,4))
        self.assertEqual(session.kind,"REGULAR")
        self.assertEqual(session.close_time,time(16,0))
        self.assertEqual(session.closes_at.utcoffset(),timedelta(hours=-5))
        self.assertEqual(session.calendar_contract,CALENDAR_CONTRACT)
        self.assertNotEqual(session.calendar_revision,"unknown")

    def test_2026_early_closes_both_1300(self):
        for d in (date(2026,11,27),date(2026,12,24)):
            with self.subTest(d=d):
                session=equity_session(d)
                self.assertEqual(session.kind,"EARLY_CLOSE")
                self.assertEqual(session.close_time,time(13,0))
                self.assertEqual(reminder_due_at(d).time(),time(13,10))

    def test_2027_early_close(self):
        session=equity_session(date(2027,11,26))
        self.assertEqual(session.close_time,time(13,0))

    def test_2028_early_close_with_dst(self):
        session=equity_session(date(2028,7,3))
        self.assertEqual(session.close_time,time(13,0))
        self.assertEqual(session.closes_at.utcoffset(),timedelta(hours=-4))

    def test_thanksgiving_and_observed_july_4_holidays(self):
        for d in (date(2026,11,26),date(2026,7,3),date(2026,12,25)):
            with self.subTest(d=d):
                self.assertFalse(equity_session(d).is_open)
                with self.assertRaisesRegex(ValueError,"not an exchange"):
                    require_exchange_session(d)

    def test_weekends_closed(self):
        self.assertFalse(equity_session(date(2026,11,7)).is_open)
        self.assertFalse(equity_session(date(2026,11,8)).is_open)

    def test_true_next_exchange_day_across_holidays(self):
        self.assertEqual(next_exchange_session(date(2026,11,25)),date(2026,11,27))
        self.assertEqual(next_exchange_session(date(2026,11,27)),date(2026,11,30))
        self.assertEqual(next_exchange_session(date(2026,12,24)),date(2026,12,28))
        self.assertEqual(next_exchange_session(date(2026,11,4)),date(2026,11,5))
        with self.assertRaisesRegex(ValueError,"Expected next exchange session"):
            validate_next_exchange_session(date(2026,11,25),date(2026,11,26))

    def test_early_close_admits_source_volume_at_1300(self):
        d=date(2026,11,27)
        observed=datetime(2026,11,27,13,0,tzinfo=ET)
        self.assertEqual(require_postclose_source_time(d,observed).kind,"EARLY_CLOSE")
        with self.assertRaisesRegex(ValueError,"at or after 13:00 ET"):
            require_postclose_source_time(d,observed-timedelta(seconds=1))

    def test_regular_close_refuses_early_snapshot(self):
        d=date(2026,11,4)
        with self.assertRaisesRegex(ValueError,"at or after 16:00 ET"):
            require_postclose_source_time(d,datetime(2026,11,4,15,59,tzinfo=ET))

    def test_wrong_date_not_reinterpreted(self):
        d=date(2026,11,27)
        with self.assertRaisesRegex(ValueError,"next-day snapshots are not valid"):
            require_postclose_source_time(d,datetime(2026,11,28,12,0,tzinfo=ET))

    def test_naive_time_rejected(self):
        with self.assertRaisesRegex(ValueError,"timezone-aware"):
            require_postclose_source_time(date(2026,11,27),
                datetime(2026,11,27,13,5))

    def test_reminder_delay_bound_and_session_timezone(self):
        d=date(2026,11,27)
        self.assertEqual(reminder_due_at(d,minutes_after_close=10).utcoffset(),
                         timedelta(hours=-5))
        with self.assertRaisesRegex(ValueError,"Invalid reminder delay"):
            reminder_due_at(d,minutes_after_close=-1)
