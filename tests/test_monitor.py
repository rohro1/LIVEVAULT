import datetime as dt
import json
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "engine"))

from monitor import (
    account_key,
    check_account,
    check_accounts,
    in_quiet_hours,
    main,
    parse_accounts,
    parse_live_status,
    recorder_retry_delay,
)
from record import refresh_tracked_user, tracked_user


class MonitorTests(unittest.TestCase):
    def test_parse_accounts_normalizes_and_deduplicates(self):
        self.assertEqual(parse_accounts(" @Alice,alice, Bob_1 "), ["alice", "bob_1"])

    def test_parse_accounts_rejects_invalid_username(self):
        with self.assertRaises(ValueError):
            parse_accounts("valid,has space")

    def test_parse_live_status_detects_live_and_offline_accounts(self):
        self.assertTrue(parse_live_status({"data": {"user": {"status": 2}}}))
        self.assertFalse(parse_live_status({"data": {"user": {"status": 4}}}))

    def test_parse_live_status_accepts_status_three_with_active_stream_data(self):
        stream_data = {
            "data": {
                "origin": {
                    "main": {"flv": "https://example.test/stream"},
                },
            },
        }
        response = {
            "data": {
                "user": {"status": 3, "roomId": "123"},
                "liveRoom": {
                    "status": 3,
                    "streamData": {
                        "pull_data": {"stream_data": json.dumps(stream_data)},
                    },
                },
            },
        }
        self.assertTrue(parse_live_status(response))

    def test_parse_live_status_rejects_status_three_without_active_stream_data(self):
        response = {
            "data": {
                "user": {"status": 3, "roomId": "123"},
                "liveRoom": {"status": 3, "streamData": {}},
            },
        }
        self.assertFalse(parse_live_status(response))

    def test_parse_live_status_rejects_unrecognized_responses(self):
        for response in ({}, {"data": {"user": {}}}, {"data": []}):
            with self.subTest(response=response), self.assertRaises(ValueError):
                parse_live_status(response)

    def test_check_accounts_continues_after_an_account_check_fails(self):
        def check(username):
            if username == "alice":
                raise RuntimeError("TikTok unavailable")
            return True

        with patch("monitor.is_live", side_effect=check):
            results = check_accounts(["alice", "bob"])

        self.assertIsInstance(results["alice"], RuntimeError)
        self.assertTrue(results["bob"])

    def test_check_accounts_handles_no_tracked_accounts(self):
        self.assertEqual(check_accounts([]), {})

    def test_main_dispatches_a_separate_recorder_for_each_live_account(self):
        dispatched = []

        def request(url, token=None, method="GET", body=None):
            if method == "POST":
                dispatched.append(body)
                return {}
            return {"workflow_runs": []}

        environment = {
            "TRACKED": "alice,bob",
            "GITHUB_TOKEN": "token",
            "GITHUB_REPOSITORY": "owner/repo",
            "GITHUB_REF_NAME": "main",
            "HOST_KEY_SECRET": "secret",
            "QUIET_HOURS_ENABLED": "false",
        }
        with patch.dict(os.environ, environment), patch(
            "monitor.check_accounts",
            return_value={"alice": True, "bob": True},
        ), patch("monitor.request_json", side_effect=request), patch(
            "monitor.update_dashboard_status"
        ):
            main()

        self.assertEqual(len(dispatched), 2)
        self.assertEqual(
            {item["inputs"]["host_key"] for item in dispatched},
            {account_key("alice", "secret"), account_key("bob", "secret")},
        )

    def test_main_does_not_duplicate_an_active_recorder_for_one_account(self):
        dispatched = []
        alice_key = account_key("alice", "secret")

        def request(url, token=None, method="GET", body=None):
            if method == "POST":
                dispatched.append(body)
                return {}
            return {
                "workflow_runs": [{
                    "display_title": f"record-{alice_key}",
                    "status": "in_progress",
                }],
            }

        environment = {
            "TRACKED": "alice,bob",
            "GITHUB_TOKEN": "token",
            "GITHUB_REPOSITORY": "owner/repo",
            "GITHUB_REF_NAME": "main",
            "HOST_KEY_SECRET": "secret",
            "QUIET_HOURS_ENABLED": "false",
        }
        with patch.dict(os.environ, environment), patch(
            "monitor.check_accounts",
            return_value={"alice": True, "bob": True},
        ), patch("monitor.request_json", side_effect=request), patch(
            "monitor.update_dashboard_status"
        ):
            main()

        self.assertEqual(len(dispatched), 1)
        self.assertEqual(
            dispatched[0]["inputs"]["host_key"],
            account_key("bob", "secret"),
        )

    def test_main_delays_retry_after_recent_failure_but_dispatches_other_live_users(self):
        dispatched = []
        status_updates = []
        alice_key = account_key("alice", "secret")

        def request(url, token=None, method="GET", body=None):
            if method == "POST":
                dispatched.append(body)
            return {"workflow_runs": []}

        environment = {
            "TRACKED": "alice,bob",
            "GITHUB_TOKEN": "token",
            "GITHUB_REPOSITORY": "owner/repo",
            "GITHUB_REF_NAME": "main",
            "HOST_KEY_SECRET": "secret",
            "QUIET_HOURS_ENABLED": "false",
        }
        with patch.dict(os.environ, environment), patch(
            "monitor.check_accounts",
            return_value={"alice": True, "bob": True},
        ), patch("monitor.request_json", side_effect=request), patch(
            "monitor.recorder_retry_delay",
            side_effect=lambda runs, key: 120 if key == alice_key else 0,
        ), patch(
            "monitor.update_dashboard_status",
            side_effect=lambda *args: status_updates.append(args) or True,
        ):
            main()

        self.assertEqual(len(dispatched), 1)
        self.assertEqual(
            dispatched[0]["inputs"]["host_key"],
            account_key("bob", "secret"),
        )
        self.assertIn(
            ("alice", True, "error"),
            {update[:3] for update in status_updates},
        )

    def test_recorder_retry_delay_uses_bounded_exponential_backoff(self):
        now = dt.datetime(2026, 11, 8, 12, 0, tzinfo=dt.timezone.utc)
        host_key = account_key("alice", "secret")
        runs = [
            {
                "display_title": f"record-{host_key}",
                "status": "completed",
                "conclusion": "failure",
                "updated_at": "2026-11-08T11:59:00Z",
            },
            {
                "display_title": f"record-{host_key}",
                "status": "completed",
                "conclusion": "failure",
                "updated_at": "2026-11-08T11:50:00Z",
            },
        ]

        self.assertEqual(recorder_retry_delay(runs, host_key, now), 9 * 60)
        self.assertEqual(recorder_retry_delay(runs[:1], host_key, now), 4 * 60)
        self.assertEqual(
            recorder_retry_delay(
                [{
                    **runs[0],
                    "conclusion": "success",
                }],
                host_key,
                now,
            ),
            0,
        )

    def test_check_account_retries_a_transient_request_failure(self):
        with patch(
            "monitor.is_live",
            side_effect=[RuntimeError("temporary failure"), True],
        ) as is_live_mock, patch("monitor.time.sleep"):
            result = check_account("alice")

        self.assertTrue(result)
        self.assertEqual(is_live_mock.call_count, 2)

    def test_account_key_is_stable_and_secret_bound(self):
        self.assertEqual(account_key("alice", "secret"), account_key("alice", "secret"))
        self.assertNotEqual(account_key("alice", "secret"), account_key("alice", "other"))

    def test_tracked_user_resolves_opaque_key(self):
        key = account_key("alice", "secret")
        self.assertEqual(tracked_user(key, "bob, alice", "secret"), "alice")
        self.assertIsNone(tracked_user(key, "bob", "secret"))

    def test_recorder_uses_injected_tracked_variable_without_api_access(self):
        key = account_key("alice", "secret")
        with patch.dict("os.environ", {"TRACKED": "alice,bob"}), patch(
            "record.github_request",
            side_effect=AssertionError("unexpected GitHub API call"),
        ):
            self.assertEqual(refresh_tracked_user(key, "repo", "token", "secret"), "alice")

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
