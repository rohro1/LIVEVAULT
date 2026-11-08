import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "engine"))

from drive_admin import backfill_durations, delete_recording


class DriveAdminTests(unittest.TestCase):
    def test_backfill_saves_exact_duration_from_drive_video_metadata(self):
        requests = []
        recording = {
            "drive_id": "drive_file_12345",
            "filename": "alice_room_20261108_120000_part001.mp4",
        }

        def dashboard_request(path, method="GET", body=None):
            requests.append((path, method, body))
            if path.endswith("missing-duration"):
                return {"recordings": [recording]}
            return {"ok": True}

        with patch("drive_admin.dashboard_request", side_effect=dashboard_request), patch(
            "drive_admin.drive_file",
            return_value={
                "id": recording["drive_id"],
                "name": recording["filename"],
                "trashed": False,
                "videoMediaMetadata": {"durationMillis": "302450"},
            },
        ):
            backfill_durations("access-token")

        self.assertEqual(
            requests[-1],
            (
                "/api/internal/recordings/drive_file_12345",
                "PATCH",
                {"duration_seconds": 302.45},
            ),
        )

    def test_delete_requires_drive_metadata_to_match_dashboard_record(self):
        recording = {
            "drive_id": "drive_file_12345",
            "filename": "alice_room_20261108_120000_part001.mp4",
        }
        requests = []

        def dashboard_request(path, method="GET", body=None):
            requests.append((path, method))
            return recording if method == "GET" else {"ok": True}

        with patch("drive_admin.dashboard_request", side_effect=dashboard_request), patch(
            "drive_admin.drive_file",
            return_value={
                "id": recording["drive_id"],
                "name": recording["filename"],
                "trashed": False,
            },
        ), patch("drive_admin.api_request", return_value=(204, {}, b"")) as drive_request:
            delete_recording(recording["drive_id"], "access-token")

        self.assertEqual(drive_request.call_args.kwargs["method"], "DELETE")
        self.assertEqual(requests[-1], (
            "/api/internal/recordings/drive_file_12345",
            "DELETE",
        ))

    def test_delete_rejects_mismatched_drive_file_metadata(self):
        recording = {
            "drive_id": "drive_file_12345",
            "filename": "alice_room_20261108_120000_part001.mp4",
        }
        with patch("drive_admin.dashboard_request", return_value=recording), patch(
            "drive_admin.drive_file",
            return_value={"id": recording["drive_id"], "name": "unrelated.mp4"},
        ), patch("drive_admin.api_request") as drive_request, self.assertRaisesRegex(
            RuntimeError, "did not match the dashboard recording"
        ):
            delete_recording(recording["drive_id"], "access-token")

        drive_request.assert_not_called()


if __name__ == "__main__":
    unittest.main()
