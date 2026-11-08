import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "engine"))
import resolve


def gated_response():
    return {
        "data": {
            "user": {"status": 2, "roomId": 123},
            "liveRoom": {"status": 2, "startTime": 0, "title": "x", "liveRoomMode": 0},
        }
    }


def working_response():
    return {
        "data": {
            "user": {"status": 2, "roomId": 456},
            "liveRoom": {
                "status": 2, "startTime": 1700000000,
                "streamData": {"pull_data": {"stream_data":
                    '{"data": {"origin": {"main": {"flv": "https://example.com/x.flv"}}}}'}},
            },
        }
    }


class GatedAccountTests(unittest.TestCase):
    @patch("resolve.time.sleep", return_value=None)
    @patch("resolve.get_json")
    def test_gated_account_fails_fast_with_clear_message(self, mock_get, mock_sleep):
        mock_get.return_value = gated_response()
        with self.assertRaises(SystemExit) as ctx:
            with patch("sys.argv", ["resolve.py", "someuser", "/tmp/out.url"]):
                resolve.main()
        self.assertEqual(ctx.exception.code, 3)
        self.assertEqual(mock_get.call_count, resolve.GATED_ATTEMPTS)
        self.assertEqual(mock_sleep.call_count, resolve.GATED_ATTEMPTS - 1)

    @patch("resolve.time.sleep", return_value=None)
    @patch("resolve.get_json")
    def test_working_account_is_unaffected(self, mock_get, mock_sleep):
        mock_get.return_value = working_response()
        with patch("sys.argv", ["resolve.py", "someuser", "/tmp/out.url"]):
            resolve.main()
        self.assertEqual(mock_get.call_count, 1)
        self.assertEqual(mock_sleep.call_count, 0)

    @patch("resolve.time.sleep", return_value=None)
    @patch("resolve.get_json")
    def test_account_with_stream_keys_but_transient_empty_data_still_gets_full_retries(self, mock_get, mock_sleep):
        resp = {
            "data": {
                "user": {"status": 2, "roomId": 789},
                "liveRoom": {"status": 2, "startTime": 1700000000, "streamData": {}},
            }
        }
        mock_get.return_value = resp
        with self.assertRaises(SystemExit) as ctx:
            with patch("sys.argv", ["resolve.py", "someuser", "/tmp/out.url"]):
                resolve.main()
        self.assertEqual(ctx.exception.code, 3)
        self.assertEqual(mock_get.call_count, resolve.STREAM_ATTEMPTS)
        self.assertEqual(mock_sleep.call_count, resolve.STREAM_ATTEMPTS - 1)


if __name__ == "__main__":
    unittest.main()
