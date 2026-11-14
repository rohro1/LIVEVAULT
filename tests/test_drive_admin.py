import io
import json
import os
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "engine"))

from drive_admin import (
    backfill_durations,
    consolidate_drive_folders,
    dashboard_request,
    delete_recording,
    drive_file,
    move_drive_item,
    organize_recordings,
    probe_drive_duration,
)


class DriveAdminTests(unittest.TestCase):
    def test_dashboard_request_uses_ingest_bearer_token(self):
        with patch.dict(
            os.environ,
            {
                "DASHBOARD_API_URL": "https://livevault.example",
                "DASHBOARD_API_TOKEN": "dashboard-token",
            },
        ), patch(
            "drive_admin.api_request",
            return_value=(200, {}, b"{\"recordings\":[]}"),
        ) as request:
            dashboard_request("/api/internal/recordings/missing-duration")

        self.assertEqual(
            request.call_args.kwargs["headers"]["Authorization"],
            "Bearer dashboard-token",
        )

    def test_drive_file_uses_google_access_token(self):
        with patch(
            "drive_admin.api_request",
            return_value=(200, {}, b"{\"id\":\"drive_file_12345\"}"),
        ) as request:
            drive_file("drive_file_12345", "access-token")

        self.assertEqual(
            request.call_args.kwargs["headers"]["Authorization"],
            "Bearer access-token",
        )

    def test_move_drive_item_verifies_parent_change(self):
        item = {
            "id": "drive_file_12345",
            "name": "part001.mp4",
            "parents": ["duplicate-folder"],
        }
        with patch(
            "drive_admin.api_request",
            return_value=(
                200,
                {},
                json.dumps({
                    "id": item["id"],
                    "name": item["name"],
                    "parents": ["canonical-folder"],
                    "trashed": False,
                }).encode(),
            ),
        ) as move_request:
            move_drive_item(item, "duplicate-folder", "canonical-folder", "access-token")

        self.assertIn("addParents=canonical-folder", move_request.call_args.args[0])
        self.assertIn("removeParents=duplicate-folder", move_request.call_args.args[0])

    def test_video_duration_probe_downloads_from_drive_and_uses_ffprobe(self):
        response = io.BytesIO(b"video data")

        def run_ffprobe(command, **kwargs):
            with open(command[-1], "rb") as video:
                self.assertEqual(video.read(), b"video data")
            return SimpleNamespace(returncode=0, stdout="302.456\n")

        with patch("drive_admin.urllib.request.urlopen", return_value=response) as download, patch(
            "drive_admin.subprocess.run", side_effect=run_ffprobe
        ):
            duration = probe_drive_duration("drive_file_12345", "access-token")

        self.assertEqual(duration, 302.456)
        self.assertIn("alt=media", download.call_args.args[0].full_url)
        self.assertEqual(
            download.call_args.args[0].headers["Authorization"],
            "Bearer access-token",
        )

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

    def test_backfill_probes_video_when_drive_metadata_has_no_duration(self):
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
            },
        ), patch("drive_admin.probe_drive_duration", return_value=302.456) as probe:
            backfill_durations("access-token")

        probe.assert_called_once_with(recording["drive_id"], "access-token")
        self.assertEqual(
            requests[-1],
            (
                "/api/internal/recordings/drive_file_12345",
                "PATCH",
                {"duration_seconds": 302.456},
            ),
        )

    def test_organize_moves_only_verified_dashboard_recordings_to_date_folder(self):
        recording = {
            "drive_id": "drive_file_12345",
            "username": "alice",
            "started_at": "20261108_120000",
            "filename": "alice_room_20261108_120000_part001.mp4",
        }
        with patch(
            "drive_admin.dashboard_request",
            side_effect=lambda path, **_: (
                {"usernames": ["alice"]}
                if path == "/api/internal/accounts"
                else {"recordings": [recording]}
            ),
        ), patch(
            "drive_admin.drive_file",
            return_value={
                "id": recording["drive_id"],
                "name": recording["filename"],
                "trashed": False,
                "parents": ["old-folder"],
            },
        ), patch(
            "drive_admin.recording_folder_id",
            return_value="date-folder",
        ), patch(
            "drive_admin.drive_folders",
            return_value=[{
                "id": "date-folder",
                "name": "2026-11-08",
                "mimeType": "application/vnd.google-apps.folder",
                "parents": ["user-folder"],
            }],
        ), patch(
            "drive_admin.api_request",
            return_value=(
                200,
                {},
                json.dumps({
                    "id": recording["drive_id"],
                    "name": recording["filename"],
                    "parents": ["date-folder"],
                    "trashed": False,
                }).encode(),
            ),
        ) as move_request, patch(
            "drive_admin.consolidate_drive_folders",
            return_value=(0, 0, 0, 0),
        ) as consolidate:
            organize_recordings("access-token")

        move_url = move_request.call_args.args[0]
        self.assertIn("addParents=date-folder", move_url)
        self.assertIn("removeParents=old-folder", move_url)
        self.assertEqual(move_request.call_args.kwargs["method"], "PATCH")
        consolidate.assert_called_once_with("access-token", ["alice"], ["alice"])

    def test_organize_skips_files_already_in_their_destination_folder(self):
        recording = {
            "drive_id": "drive_file_12345",
            "username": "alice",
            "started_at": "20261108_120000",
            "filename": "alice_room_20261108_120000_part001.mp4",
        }
        with patch(
            "drive_admin.dashboard_request",
            side_effect=lambda path, **_: (
                {"usernames": ["alice"]}
                if path == "/api/internal/accounts"
                else {"recordings": [recording]}
            ),
        ), patch(
            "drive_admin.drive_file",
            return_value={
                "id": recording["drive_id"],
                "name": recording["filename"],
                "trashed": False,
                "parents": ["date-folder"],
            },
        ), patch(
            "drive_admin.recording_folder_id",
            return_value="date-folder",
        ), patch(
            "drive_admin.drive_folders",
            return_value=[{
                "id": "date-folder",
                "name": "2026-11-08",
                "mimeType": "application/vnd.google-apps.folder",
                "parents": ["user-folder"],
            }],
        ), patch("drive_admin.api_request") as move_request, patch(
            "drive_admin.consolidate_drive_folders",
            return_value=(0, 0, 0, 0),
        ):
            organize_recordings("access-token")

        move_request.assert_not_called()

    def test_consolidate_merges_duplicate_user_and_root_folders(self):
        root = {
            "id": "root-folder",
            "name": "LIVEVAULT",
            "mimeType": "application/vnd.google-apps.folder",
            "parents": ["root"],
        }
        duplicate_root = {
            "id": "duplicate-root",
            "name": "LIVEVAULT",
            "mimeType": "application/vnd.google-apps.folder",
            "parents": ["root"],
        }
        canonical_user = {
            "id": "user-folder",
            "name": "alice",
            "mimeType": "application/vnd.google-apps.folder",
            "parents": ["root-folder"],
        }
        duplicate_user = {
            "id": "duplicate-user",
            "name": "alice",
            "mimeType": "application/vnd.google-apps.folder",
            "parents": ["duplicate-root"],
        }
        canonical_date = {
            "id": "date-folder",
            "name": "2026-11-08",
            "mimeType": "application/vnd.google-apps.folder",
            "parents": ["user-folder"],
        }
        duplicate_date = {
            "id": "duplicate-date",
            "name": "2026-11-08",
            "mimeType": "application/vnd.google-apps.folder",
            "parents": ["duplicate-user"],
        }
        file = {
            "id": "recording-file",
            "name": "alice_part001.mp4",
            "mimeType": "video/mp4",
            "parents": ["duplicate-date"],
        }

        def children(folder_id, _token):
            return {
                "root-folder": [canonical_user],
                "duplicate-root": [duplicate_user],
                "user-folder": [canonical_date],
                "duplicate-user": [duplicate_date],
                "date-folder": [],
                "duplicate-date": [file],
            }.get(folder_id, [])

        def ensure_folder(name, _token, parent_id=None):
            return {
                ("alice", "root-folder"): "user-folder",
                ("2026-11-08", "user-folder"): "date-folder",
            }[(name, parent_id)]

        with patch(
            "drive_admin.drive_folders",
            return_value=[root, duplicate_root],
        ), patch(
            "drive_admin.drive_children",
            side_effect=children,
        ), patch(
            "drive_admin.ensure_drive_folder",
            side_effect=ensure_folder,
        ), patch("drive_admin.move_drive_item") as move_item, patch(
            "drive_admin.trash_empty_folder",
            return_value=True,
        ) as trash:
            result = consolidate_drive_folders("access-token", ["alice"], [])

        self.assertEqual(result, (3, 0, 0, 0))
        move_item.assert_called_once_with(file, "duplicate-date", "date-folder", "access-token")
        self.assertEqual(trash.call_count, 3)

    def test_consolidate_creates_user_folder_for_dashboard_account(self):
        root = {
            "id": "root-folder",
            "name": "LIVEVAULT",
            "mimeType": "application/vnd.google-apps.folder",
            "parents": ["root"],
        }
        with patch(
            "drive_admin.drive_folders",
            return_value=[root],
        ), patch(
            "drive_admin.drive_children",
            side_effect=[
                [],
                [{
                    "id": "user-folder",
                    "name": "alice",
                    "mimeType": "application/vnd.google-apps.folder",
                    "parents": ["root-folder"],
                }],
                [],
            ],
        ), patch(
            "drive_admin.ensure_drive_folder",
            side_effect=lambda name, _token, parent_id=None: (
                "user-folder" if name == "alice" else "date-folder"
            ),
        ) as ensure_folder, patch(
            "drive_admin.trash_empty_folder",
            return_value=True,
        ):
            result = consolidate_drive_folders("access-token", ["alice"], [])

        self.assertEqual(result, (0, 0, 0, 0))
        ensure_folder.assert_called_once_with("alice", "access-token", "root-folder")

    def test_consolidate_preserves_duplicate_folders_with_contents(self):
        root = {
            "id": "root-folder",
            "name": "LIVEVAULT",
            "mimeType": "application/vnd.google-apps.folder",
            "parents": ["root"],
        }
        duplicate_root = {
            "id": "duplicate-root",
            "name": "LIVEVAULT",
            "mimeType": "application/vnd.google-apps.folder",
            "parents": ["root"],
        }
        canonical_user = {
            "id": "user-folder",
            "name": "alice",
            "mimeType": "application/vnd.google-apps.folder",
            "parents": ["root-folder"],
        }
        duplicate_user = {
            "id": "duplicate-user",
            "name": "alice",
            "mimeType": "application/vnd.google-apps.folder",
            "parents": ["duplicate-root"],
        }
        with patch(
            "drive_admin.drive_folders",
            return_value=[root, duplicate_root],
        ), patch(
            "drive_admin.drive_children",
            side_effect=lambda folder_id, _token: {
                "root-folder": [canonical_user],
                "duplicate-root": [duplicate_user],
                "user-folder": [],
                "duplicate-user": [{"id": "untracked-item"}],
            }.get(folder_id, []),
        ), patch(
            "drive_admin.ensure_drive_folder",
            side_effect=lambda name, _token, parent_id=None: (
            "user-folder" if name == "alice" else "date-folder"
            ),
        ), patch(
            "drive_admin.trash_empty_folder",
            return_value=False,
        ) as trash:
            result = consolidate_drive_folders("access-token", ["alice"], [])

        self.assertEqual(result, (0, 2, 0, 0))
        self.assertEqual(trash.call_count, 2)

    def test_consolidate_removes_only_empty_folder_for_removed_account(self):
        root = {
            "id": "root-folder",
            "name": "LIVEVAULT",
            "mimeType": "application/vnd.google-apps.folder",
            "parents": ["root"],
        }
        user = {
            "id": "user-folder",
            "name": "alice",
            "mimeType": "application/vnd.google-apps.folder",
            "parents": ["root-folder"],
        }
        date = {
            "id": "date-folder",
            "name": "2026-11-08",
            "mimeType": "application/vnd.google-apps.folder",
            "parents": ["user-folder"],
        }
        with patch(
            "drive_admin.drive_folders",
            return_value=[root],
        ), patch(
            "drive_admin.drive_children",
            side_effect=lambda folder_id, _token: {
                "root-folder": [user],
                "user-folder": [date],
                "date-folder": [],
            }.get(folder_id, []),
        ), patch(
            "drive_admin.ensure_drive_folder",
                side_effect=lambda name, _token, parent_id=None: (
                    "user-folder" if name == "alice" else "date-folder"
                ),
        ), patch(
            "drive_admin.trash_empty_folder",
            return_value=True,
        ) as trash:
            result = consolidate_drive_folders("access-token", [], [])

        self.assertEqual(result, (0, 0, 2, 0))
        self.assertEqual(trash.call_count, 2)

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
