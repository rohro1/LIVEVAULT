import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "engine"))

from record import (
    api_request,
    capture_has_data,
    ffmpeg_error_summary,
    resolve_stream,
    save_failure_reason,
    upload_segments,
)


class RecorderTests(unittest.TestCase):
    def test_api_requests_use_livevault_user_agent(self):
        response = Mock()
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        response.status = 200
        response.headers = {}
        response.read.return_value = b"{}"

        with patch("record.urllib.request.urlopen", return_value=response) as urlopen:
            api_request("https://example.test/api", headers={"Authorization": "Bearer test"})

        request = urlopen.call_args.args[0]
        self.assertEqual(request.get_header("User-agent"), "LIVEVAULT/1.0")

    def test_capture_requires_nonempty_segment_data(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            directory = Path(temp_dir)
            segment = directory / "part_001.ts"
            segment.touch()
            self.assertFalse(capture_has_data(directory))

            segment.write_bytes(b"stream data")
            self.assertTrue(capture_has_data(directory))

    def test_uploaded_recording_reports_its_probed_duration(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            directory = Path(temp_dir)
            (directory / "part_001.ts").write_bytes(b"stream data")

            def convert_segment(command, **_kwargs):
                Path(command[-1]).write_bytes(b"mp4 data")
                return Mock(returncode=0)

            verified = {
                "id": "drive-id",
                "size": "8",
                "webViewLink": "https://drive.google.com/file/d/drive-id/view",
            }
            with patch("record.subprocess.run", side_effect=convert_segment), patch(
                "record.probe_duration", return_value=1800.25
            ), patch("record.drive_upload", return_value=verified), patch(
                "record.notify_dashboard"
            ) as notify_dashboard:
                next_part = upload_segments(
                    directory, "alice", "room", "20261108_120000", 1, finalized=True
                )

        self.assertEqual(next_part, 2)
        self.assertEqual(
            notify_dashboard.call_args.args,
            (
                "alice",
                "room",
                "20261108_120000",
                1,
                "alice_room_20261108_120000_part001.mp4",
                1800.25,
                verified,
            ),
        )

    def test_ffmpeg_error_summary_redacts_source_urls(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            log = Path(temp_dir) / "ffmpeg.log"
            log.write_text("Unable to open https://example.test/live?token=secret\n")

            summary = ffmpeg_error_summary(log)

        self.assertIn("[URL]", summary)
        self.assertNotIn("secret", summary)

    def test_resolver_error_is_preserved_for_failure_reporting(self):
        result = Mock(returncode=3, stdout=(
            "RESULT: resolver error: TikTok stream request returned HTTP 403 after 6 checks\n"
        ))
        with patch("record.subprocess.run", return_value=result), self.assertRaises(
            RuntimeError,
        ) as error:
            resolve_stream("alice", Path("."))

        self.assertIn("HTTP 403", str(error.exception))

    def test_failure_reason_is_flattened_and_urls_are_redacted(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            env_file = Path(temp_dir) / "github_env"
            with patch.dict("os.environ", {"GITHUB_ENV": str(env_file)}):
                save_failure_reason(
                    RuntimeError("Stream failed\nhttps://example.test/live?token=secret")
                )

            saved = env_file.read_text()

        self.assertIn("Stream failed [URL]", saved)
        self.assertNotIn("\nhttps://", saved)
        self.assertNotIn("secret", saved)


if __name__ == "__main__":
    unittest.main()
