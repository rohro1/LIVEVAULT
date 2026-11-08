import datetime as dt
import sys
import unittest
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "engine"))

from monitor import account_key, in_quiet_hours, parse_accounts
from record import tracked_user


class MonitorTests(unittest.TestCase):
    def test_parse_accounts_normalizes_and_deduplicates(self):
        self.assertEqual(parse_accounts(" @Alice,alice, Bob_1 "), ["alice", "bob_1"])

    def test_parse_accounts_rejects_invalid_username(self):
        with self.assertRaises(ValueError):
            parse_accounts("valid,has space")

    def test_account_key_is_stable_and_secret_bound(self):
        self.assertEqual(account_key("alice", "secret"), account_key("alice", "secret"))
        self.assertNotEqual(account_key("alice", "secret"), account_key("alice", "other"))

    def test_tracked_user_resolves_opaque_key(self):
        key = account_key("alice", "secret")
        self.assertEqual(tracked_user(key, "bob, alice", "secret"), "alice")
        self.assertIsNone(tracked_user(key, "bob", "secret"))

    def test_quiet_hours_are_disabled_by_default(self):
        self.assertFalse(in_quiet_hours("false", "", ""))

    def test_quiet_hours_cover_daytime_and_midnight_ranges(self):
        et = ZoneInfo("America/New_York")
        early_morning = dt.datetime(2026, 11, 7, 5, 0, tzinfo=et)
        daytime = dt.datetime(2026, 11, 7, 12, 0, tzinfo=et)
        overnight = dt.datetime(2026, 11, 7, 23, 0, tzinfo=et)
        self.assertTrue(in_quiet_hours("true", "04:00", "06:00", early_morning))
        self.assertTrue(in_quiet_hours("true", "22:00", "06:00", overnight))
        self.assertFalse(in_quiet_hours("true", "22:00", "06:00", daytime))

    def test_quiet_hours_reject_equal_boundaries(self):
        with self.assertRaises(ValueError):
            in_quiet_hours("true", "04:00", "04:00")

    def test_quiet_hours_reject_invalid_configuration(self):
        with self.assertRaises(ValueError):
            in_quiet_hours("enabled", "04:00", "08:00")
        with self.assertRaises(ValueError):
            in_quiet_hours("true", "4:00", "08:00")


if __name__ == "__main__":
    unittest.main()
