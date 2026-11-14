import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, call, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "engine"))

from record import (
    APIError,
    CaptureStartupError,
    account_key,
    api_request,
    capture_has_data,
    drive_upload,
    ffmpeg_error_summary,
    recording_folder_id,
    refresh_tracked_user,
    resolve_stream,
    run_capture,
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

    def test_ffmpeg_startup_failure_is_retryable_and_redacts_stream_url(self):
        class FailedProcess:
            returncode = 1

            def poll(self):
                return self.returncode

        def start_failed_process(command, **_kwargs):
            (Path(command[-1]).parent / "ffmpeg.log").write_text(
                "Error opening https://example.test/stream?token=secret: Server returned 5XX"
            )
            return FailedProcess()

        with patch("record.subprocess.Popen", side_effect=start_failed_process), patch(
            "record.time.monotonic", return_value=100
        ), patch("record.upload_segments", return_value=1), self.assertRaises(
            CaptureStartupError
        ) as error:
            run_capture(
                "https://example.test/stream?token=secret",
                "room",
                "alice",
                "20261108_120000",
                30,
                1,
                1000,
                "host-key",
                "owner/repo",
                "token",
                "secret",
            )

        self.assertIn("Server returned 5XX", str(error.exception))
        self.assertNotIn("secret", str(error.exception))

    def test_main_resolves_a_fresh_stream_after_ffmpeg_startup_failure(self):
        environment = {
            "GITHUB_REPOSITORY": "owner/repo",
            "GITHUB_TOKEN": "github-token",
            "HOST_KEY_SECRET": "host-secret",
            "GITHUB_REF_NAME": "main",
            "QUIET_HOURS_ENABLED": "false",
        }
        with patch.dict("record.os.environ", environment), patch(
            "record.sys.argv",
            ["record.py", "a" * 64, "1", "20261108_120000", "30"],
        ), patch("record.time.monotonic", side_effect=[
            100, 100, 100, 100, 100, 345100,
        ]), patch("record.time.sleep") as sleep, patch(
            "record.refresh_tracked_user", return_value="alice"
        ), patch("record.in_quiet_hours", return_value=False), patch(
            "record.resolve_stream", return_value=("https://example.test/stream", "room")
        ) as resolve, patch("record.notify_dashboard_status") as notify, patch(
            "record.run_capture",
            side_effect=[CaptureStartupError("Server returned 5XX"), (1, None)],
        ) as capture, patch("record.dispatch_handoff") as handoff:
            from record import main

            main()

        self.assertEqual(capture.call_count, 2)
        self.assertEqual(resolve.call_count, 4)
        self.assertTrue(any(
            call.args[3] == "Stream connection failed before recording data; retrying shortly"
            for call in notify.call_args_list
        ))
        self.assertTrue(handoff.called)
        self.assertGreaterEqual(sleep.call_count, 2)

    def test_removed_account_is_detected_from_live_dashboard_state(self):
        host_key = account_key("alice", "secret")
        environment = {
            "TRACKED": "alice",
            "DASHBOARD_API_URL": "https://livevault.example",
            "DASHBOARD_API_TOKEN": "dashboard-token",
        }
        with patch.dict("os.environ", environment), patch(
            "record.api_request",
            return_value=(200, {}, b'{"active":false}'),
        ) as request:
            username = refresh_tracked_user(
                host_key,
                "owner/repo",
                "github-token",
                "secret",
            )

        self.assertIsNone(username)
        self.assertEqual(
            request.call_args.args[0],
            "https://livevault.example/api/internal/accounts/alice",
        )

    def test_tracked_account_check_fails_explicitly_when_dashboard_is_unavailable(self):
        with patch.dict(
            "os.environ",
            {
                "TRACKED": "alice",
                "DASHBOARD_API_URL": "https://livevault.example",
                "DASHBOARD_API_TOKEN": "dashboard-token",
            },
        ), patch("record.api_request", side_effect=APIError(502)), self.assertRaisesRegex(
            APIError, "HTTP 502"
        ):
            refresh_tracked_user(
                account_key("alice", "secret"),
                "owner/repo",
                "github-token",
                "secret",
            )

    def test_uploaded_recording_reports_its_probed_duration(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            directory = Path(temp_dir)
            (directory / "part_001.ts").write_bytes(b"stream data")

            def convert_segment(command, **_kwargs):
                Path(command[-1]).write_bytes(b"mp4 data")
                return Mock(returncode=1)

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

    def test_drive_chunk_retries_a_transient_gateway_error(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "part.mp4"
            path.write_bytes(b"recording data")
            responses = [
                (200, {"Location": "https://upload.example/session"}, b""),
                APIError(502),
                (308, {}, b""),
                (200, {}, b'{"id":"drive-id"}'),
                (200, {}, (
                    b'{"id":"drive-id","name":"part.mp4","size":"14",'
                    b'"webViewLink":"https://drive.google.com/file/d/drive-id/view",'
                    b'"trashed":false,"parents":["folder-id"]}'
                )),
            ]
            with patch("record.refresh_token", return_value="access-token"), patch(
                "record.recording_folder_id", return_value="folder-id"
            ), patch(
                "record.api_request", side_effect=responses
            ) as request, patch("record.time.sleep"):
                verified = drive_upload(
                    path, "part.mp4", "alice", "20261108_120000"
                )

        self.assertEqual(verified["id"], "drive-id")
        self.assertEqual(request.call_count, 5)
        self.assertEqual(
            json.loads(request.call_args_list[0].kwargs["body"])["parents"],
            ["folder-id"],
        )

    def test_recording_folder_uses_livevault_username_and_new_york_date_names(self):
        with patch(
            "record.ensure_drive_folder",
            side_effect=["root-id", "user-id", "date-id"],
        ) as ensure_folder:
            folder_id = recording_folder_id("alice", "20261108_120000", "token")

        self.assertEqual(folder_id, "date-id")
        self.assertEqual(
            ensure_folder.call_args_list,
            [
                call("LIVEVAULT", "token"),
                call("alice", "token", "root-id"),
                call("2026-11-08", "token", "user-id"),
            ],
        )

    def test_recording_folder_rechecks_drive_path_for_each_upload(self):
        with patch(
            "record.ensure_drive_folder",
            side_effect=[
                "root-id", "user-id", "old-date-id",
                "root-id", "user-id", "canonical-date-id",
            ],
        ) as ensure_folder:
            old_folder = recording_folder_id("alice", "20261108_120000", "token")
            canonical_folder = recording_folder_id("alice", "20261108_120000", "token")

        self.assertEqual(old_folder, "old-date-id")
        self.assertEqual(canonical_folder, "canonical-date-id")
        self.assertEqual(ensure_folder.call_count, 6)

    def test_upload_segments_attempts_later_parts_after_one_upload_fails(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            directory = Path(temp_dir)
            first = directory / "part_001.ts"
            second = directory / "part_002.ts"
            first.write_bytes(b"first")
            second.write_bytes(b"second")
            upload_results = [
                RuntimeError("Google Drive upload returned HTTP 502"),
                {
                    "id": "drive-id",
                    "size": "8",
                    "webViewLink": "https://drive.google.com/file/d/drive-id/view",
                },
            ]

            def convert_segment(command, **_kwargs):
                Path(command[-1]).write_bytes(b"mp4 data")
                return Mock(returncode=0)

            with patch("record.subprocess.run", side_effect=convert_segment), patch(
                "record.probe_duration", return_value=30
            ), patch("record.drive_upload", side_effect=upload_results) as drive_upload, patch(
                "record.notify_dashboard"
            ), self.assertRaisesRegex(RuntimeError, "part 001.*HTTP 502"):
                upload_segments(directory, "alice", "room", "20261108_120000", 1, True)
            first_exists = first.exists()
            second_exists = second.exists()

        self.assertEqual(drive_upload.call_count, 2)
        self.assertTrue(first_exists)
        self.assertFalse(second_exists)

    def test_drive_upload_error_finalizes_and_retries_captured_segments(self):
        class FakeProcess:
            returncode = None

            def poll(self):
                return self.returncode

            def send_signal(self, _signal):
                self.returncode = 0

            def wait(self, timeout=None):
                return self.returncode

        process = FakeProcess()
        with patch("record.subprocess.Popen", return_value=process), patch(
            "record.time.monotonic", return_value=100
        ), patch("record.capture_has_data", return_value=True), patch(
            "record.notify_dashboard_status"
        ), patch(
            "record.refresh_tracked_user", return_value="alice"
        ), patch(
            "record.upload_segments",
            side_effect=[RuntimeError("Google Drive upload chunk failed (HTTP 502)"), 2],
        ) as upload_segments, self.assertRaisesRegex(
            RuntimeError, "Google Drive upload chunk failed \\(HTTP 502\\)"
        ):
            run_capture(
                "https://example.test/stream",
                "room",
                "alice",
                "20261108_120000",
                30,
                1,
                1000,
                "host-key",
                "owner/repo",
                "token",
                "secret",
            )

        self.assertEqual(upload_segments.call_count, 2)
        self.assertFalse(upload_segments.call_args_list[0].kwargs["finalized"])
        self.assertTrue(upload_segments.call_args.kwargs["finalized"])
        self.assertEqual(process.returncode, 0)

    def test_removing_account_stops_capture_and_finalizes_partial_segment(self):
        class FakeProcess:
            returncode = None

            def poll(self):
                return self.returncode

            def send_signal(self, _signal):
                self.returncode = 0

            def wait(self, timeout=None):
                return self.returncode

        process = FakeProcess()
        with patch("record.subprocess.Popen", return_value=process), patch(
            "record.time.monotonic", return_value=100
        ), patch("record.capture_has_data", return_value=True), patch(
            "record.notify_dashboard_status"
        ), patch(
            "record.refresh_tracked_user", return_value=None
        ), patch("record.upload_segments", return_value=2) as upload_segments:
            next_part, stop_reason = run_capture(
                "https://example.test/stream",
                "room",
                "alice",
                "20261108_120000",
                30,
                1,
                1000,
                "host-key",
                "owner/repo",
                "token",
                "secret",
            )

        self.assertEqual((next_part, stop_reason), (2, "untracked"))
        upload_segments.assert_called_once()
        self.assertTrue(upload_segments.call_args.kwargs["finalized"])
        self.assertEqual(process.returncode, 0)

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
