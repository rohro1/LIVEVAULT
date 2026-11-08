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
