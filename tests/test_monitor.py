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
    LiveStatus,
    account_key,
    check_account,
    check_accounts,
    in_quiet_hours,
    main,
    parse_accounts,
    recorder_retry_delay,
)
from record import refresh_tracked_user, tracked_user
from tiktok import inspect_live_status, parse_live_status


class MonitorTests(unittest.TestCase):
    def test_parse_accounts_normalizes_and_deduplicates(self):
        self.assertEqual(parse_accounts(" @Alice,alice, Bob_1 "), ["alice", "bob_1"])

    def test_parse_accounts_rejects_invalid_username(self):
        with self.assertRaises(ValueError):
            parse_accounts("valid,has space")

    def test_parse_live_status_detects_live_and_offline_accounts(self):
        possible_live = {"data": {"user": {"status": 2}}}
        self.assertIsNone(parse_live_status(possible_live))
        self.assertEqual(inspect_live_status(possible_live), LiveStatus(None, True))
        self.assertFalse(parse_live_status({"data": {"user": {"status": 4}}}))

    def test_parse_live_status_only_verifies_online_when_stream_data_is_usable(self):
        response = {
            "data": {
                "user": {"status": 2, "roomId": "123"},
                "liveRoom": {
                    "status": 2,
                    "streamData": {
                        "pull_data": {
                            "stream_data": json.dumps({
                                "data": {"origin": {"main": {
                                    "flv": "https://example.test/stream",
                                }}},
                            }),
                        },
                    },
                },
            },
        }
        self.assertTrue(parse_live_status(response))

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

    def test_parse_live_status_does_not_report_offline_without_confirmation(self):
        response = {
            "data": {
                "user": {"status": 3, "roomId": "123"},
                "liveRoom": {"status": 3, "streamData": {}},
            },
        }
        self.assertIsNone(parse_live_status(response))

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

    def test_main_queues_possible_live_but_does_not_report_it_as_verified_online(self):
        dispatched = []
        status_updates = []

        def request(url, token=None, method="GET", body=None):
            if method == "POST":
                dispatched.append(body)
                return {}
            return {"workflow_runs": []}

        environment = {
            "TRACKED": "alice",
            "GITHUB_TOKEN": "token",
            "GITHUB_REPOSITORY": "owner/repo",
            "GITHUB_REF_NAME": "main",
            "HOST_KEY_SECRET": "secret",
            "QUIET_HOURS_ENABLED": "false",
        }
        with patch.dict(os.environ, environment), patch(
            "monitor.check_accounts",
            return_value={"alice": LiveStatus(None, True)},
        ), patch("monitor.request_json", side_effect=request), patch(
            "monitor.update_dashboard_status",
            side_effect=lambda *args: status_updates.append(args) or True,
        ):
            main()

        self.assertEqual(len(dispatched), 1)
        self.assertIsNone(status_updates[-1][1])
        self.assertIn("verify the stream", status_updates[-1][3])

    def test_main_can_check_only_a_requested_tracked_account(self):
        checked = []
        environment = {
            "TRACKED": "alice,bob",
            "TARGET_USERNAME": "@bob",
            "GITHUB_TOKEN": "token",
            "GITHUB_REPOSITORY": "owner/repo",
            "GITHUB_REF_NAME": "main",
            "HOST_KEY_SECRET": "secret",
            "QUIET_HOURS_ENABLED": "false",
        }
        with patch.dict(os.environ, environment), patch(
            "monitor.check_accounts",
            side_effect=lambda accounts: checked.extend(accounts) or {"bob": False},
        ), patch("monitor.request_json", return_value={"workflow_runs": []}), patch(
            "monitor.update_dashboard_status"
        ):
            main()

        self.assertEqual(checked, ["bob"])

    def test_main_rejects_a_manual_target_not_in_tracked_accounts(self):
        environment = {
            "TRACKED": "alice,bob",
            "TARGET_USERNAME": "carol",
            "GITHUB_TOKEN": "token",
            "GITHUB_REPOSITORY": "owner/repo",
            "GITHUB_REF_NAME": "main",
            "HOST_KEY_SECRET": "secret",
            "QUIET_HOURS_ENABLED": "false",
        }
        with patch.dict(os.environ, environment), self.assertRaisesRegex(
            ValueError, "not in the tracked account list"
        ):
            main()

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

        self.assertEqual(recorder_retry_delay(runs, host_key, now), 4 * 60)
        self.assertEqual(recorder_retry_delay(runs[:1], host_key, now), 4 * 60)
        self.assertEqual(
            recorder_retry_delay(
                [{
                    **runs[0],
                    "updated_at": "2026-11-08T11:59:00Z",
                    "conclusion": "success",
                }],
                host_key,
                now,
            ),
            0,
        )

    def test_recorder_retry_delay_never_exceeds_one_monitor_interval(self):
        now = dt.datetime(2026, 11, 8, 12, 0, tzinfo=dt.timezone.utc)
        host_key = account_key("alice", "secret")
        runs = [
            {
                "display_title": f"record-{host_key}",
                "status": "completed",
                "conclusion": "failure",
                "updated_at": "2026-11-08T11:59:00Z",
            }
            for _ in range(10)
        ]

        self.assertEqual(recorder_retry_delay(runs, host_key, now), 4 * 60)

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

    def test_recorder_checks_dashboard_after_resolving_injected_tracked_variable(self):
        key = account_key("alice", "secret")
        with patch.dict("os.environ", {
            "TRACKED": "alice,bob",
            "DASHBOARD_API_URL": "https://livevault.example",
            "DASHBOARD_API_TOKEN": "dashboard-token",
        }), patch(
            "record.github_request",
            side_effect=AssertionError("unexpected GitHub API call"),
        ), patch(
            "record.api_request",
            return_value=(200, {}, b'{"active":true}'),
        ) as request:
            self.assertEqual(refresh_tracked_user(key, "repo", "token", "secret"), "alice")
        self.assertIn("/api/internal/accounts/alice", request.call_args.args[0])

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
