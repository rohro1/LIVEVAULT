import io
import json
import sys
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "engine"))

import resolve


def live_response(stream_data=None):
    live_room = {"status": 2}
    if stream_data is not None:
        live_room["streamData"] = {
            "pull_data": {"stream_data": json.dumps(stream_data)},
        }
    return {
        "data": {
            "user": {"status": 2, "roomId": "123"},
            "liveRoom": live_room,
        },
    }


class ResolveTests(unittest.TestCase):
    def test_retries_temporary_tiktok_http_failure(self):
        stream_data = {
            "data": {
                "hd": {
                    "main": {"flv": "https://example.test/live"},
                },
            },
        }
        http_error = urllib.error.HTTPError(
            "https://example.test",
            503,
            "unavailable",
            {},
            io.BytesIO(),
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            stream_path = Path(temp_dir) / "stream.url"
            with patch.object(
                sys,
                "argv",
                ["resolve.py", "alice", str(stream_path)],
            ), patch(
                "resolve.get_json",
                side_effect=[
                    http_error,
                    live_response(stream_data),
                ],
            ), patch("resolve.time.sleep") as sleep, patch.dict(
                "os.environ", {"QUALITY": "compat"}
            ):
                resolve.main()

            sleep.assert_called_once_with(resolve.STREAM_RETRY_SECONDS)
            self.assertEqual(stream_path.read_text(), "https://example.test/live")
        http_error.close()

    def test_retries_live_response_until_stream_options_are_available(self):
        stream_data = {
            "data": {
                "hd": {
                    "main": {"flv": "https://example.test/live"},
                },
            },
        }
        with tempfile.TemporaryDirectory() as temp_dir:
            stream_path = Path(temp_dir) / "stream.url"
            room_path = Path(temp_dir) / "room.id"
            with patch.object(
                sys,
                "argv",
                ["resolve.py", "alice", str(stream_path), str(room_path)],
            ), patch(
                "resolve.get_json",
                side_effect=[live_response(), live_response(stream_data)],
            ) as get_json, patch("resolve.time.sleep") as sleep, patch.dict(
                "os.environ", {"QUALITY": "compat"}
            ):
                resolve.main()

            self.assertEqual(get_json.call_count, 2)
            sleep.assert_called_once_with(resolve.STREAM_RETRY_SECONDS)
            self.assertEqual(stream_path.read_text(), "https://example.test/live")
            self.assertEqual(room_path.read_text(), "123")

    def test_retries_all_attempts_when_stream_options_never_arrive(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            with patch.object(
                sys,
                "argv",
                ["resolve.py", "alice", str(Path(temp_dir) / "stream.url")],
            ), patch(
                "resolve.get_json",
                return_value=live_response(),
            ) as get_json, patch("resolve.time.sleep") as sleep, patch.dict(
                "os.environ", {"QUALITY": "compat"}
            ), self.assertRaises(SystemExit) as exit_error:
                resolve.main()

            self.assertEqual(exit_error.exception.code, 3)
            self.assertEqual(get_json.call_count, resolve.STREAM_ATTEMPTS)
            self.assertEqual(sleep.call_count, resolve.STREAM_ATTEMPTS - 1)


if __name__ == "__main__":
    unittest.main()
