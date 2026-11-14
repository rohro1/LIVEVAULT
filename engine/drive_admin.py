import json
import math
import os
import re
import subprocess
import sys
import tempfile
import urllib.parse
import urllib.error
import urllib.request

from record import (
    APIError,
    DRIVE_API,
    api_request,
    recording_folder_id,
    refresh_token,
)


def dashboard_request(path, method="GET", body=None):
    endpoint = os.environ.get("DASHBOARD_API_URL", "").rstrip("/")
    token = os.environ.get("DASHBOARD_API_TOKEN", "")
    if not endpoint or not token:
        raise RuntimeError("Dashboard integration is not configured")
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }
    status, _, raw = api_request(
        endpoint + path,
        method=method,
        headers=headers,
        body=json.dumps(body).encode() if body is not None else None,
    )
    if status not in {200, 201, 202, 204}:
        raise RuntimeError(f"Dashboard request failed (HTTP {status})")
    return json.loads(raw) if raw else {}


def drive_file(file_id, token):
    query = urllib.parse.urlencode({
        "fields": "id,name,trashed,parents,videoMediaMetadata",
        "supportsAllDrives": "true",
    })
    url = f"{DRIVE_API}/files/{urllib.parse.quote(file_id, safe='')}?{query}"
    status, _, raw = api_request(
        url,
        headers={"Authorization": f"Bearer {token}"},
    )
    if status != 200:
        raise RuntimeError(f"Google Drive metadata request failed (HTTP {status})")
    return json.loads(raw)


def probe_drive_duration(file_id, token):
    query = urllib.parse.urlencode({
        "alt": "media",
        "supportsAllDrives": "true",
    })
    url = f"{DRIVE_API}/files/{urllib.parse.quote(file_id, safe='')}?{query}"
    request = urllib.request.Request(
        url,
        headers={
            "Authorization": f"Bearer {token}",
            "User-Agent": "LIVEVAULT/1.0",
        },
    )
    try:
        with tempfile.NamedTemporaryFile(suffix=".mp4") as video:
            with urllib.request.urlopen(request, timeout=90) as response:
                while chunk := response.read(1024 * 1024):
                    video.write(chunk)
            video.flush()
            try:
                result = subprocess.run(
                    [
                        "ffprobe",
                        "-v", "error",
                        "-show_entries", "format=duration",
                        "-of", "default=noprint_wrappers=1:nokey=1",
                        video.name,
                    ],
                    capture_output=True,
                    text=True,
                    timeout=120,
                    check=False,
                )
            except FileNotFoundError:
                raise RuntimeError("ffprobe is not installed") from None
            except subprocess.TimeoutExpired:
                raise RuntimeError("Google Drive video duration probe timed out") from None
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"Google Drive video download failed (HTTP {exc.code})") from None
    except urllib.error.URLError as exc:
        raise RuntimeError(
            f"Google Drive video download failed ({exc.reason.__class__.__name__})"
        ) from None
    if result.returncode:
        raise RuntimeError("ffprobe could not read the Google Drive video")
    try:
        duration_seconds = float(result.stdout.strip())
    except ValueError:
        raise RuntimeError("ffprobe returned an invalid video duration") from None
    if not math.isfinite(duration_seconds) or duration_seconds <= 0:
        raise RuntimeError("ffprobe returned an invalid video duration")
    return duration_seconds


def backfill_durations(token):
    updated = 0
    unavailable = 0
    checked = 0
    after = None
    while True:
        path = "/api/internal/recordings/missing-duration"
        if after:
            path += "?" + urllib.parse.urlencode({"after": after})
        recordings = dashboard_request(path).get("recordings", [])
        if not recordings:
            break
        checked += len(recordings)
        for recording in recordings:
            try:
                metadata = drive_file(recording["drive_id"], token)
                if (
                    metadata.get("id") != recording["drive_id"]
                    or metadata.get("name") != recording["filename"]
                    or metadata.get("trashed", False)
                ):
                    unavailable += 1
                    print(
                        f"WARNING: Drive file did not match the dashboard recording: "
                        f"{recording['filename']}"
                    )
                    continue
                try:
                    duration_ms = int(
                        metadata.get("videoMediaMetadata", {}).get("durationMillis", 0)
                    )
                except (TypeError, ValueError):
                    duration_ms = 0
                duration_seconds = (
                    duration_ms / 1000
                    if duration_ms > 0
                    else probe_drive_duration(recording["drive_id"], token)
                )
                dashboard_request(
                    "/api/internal/recordings/"
                    + urllib.parse.quote(recording["drive_id"], safe=""),
                    method="PATCH",
                    body={"duration_seconds": duration_seconds},
                )
                updated += 1
            except (
                APIError,
                KeyError,
                OSError,
                TypeError,
                ValueError,
                RuntimeError,
                subprocess.SubprocessError,
            ) as exc:
                unavailable += 1
                print(f"WARNING: could not fetch duration for a recording ({exc})")
        if len(recordings) < 200:
            break
        after = recordings[-1]["drive_id"]
    print(
        f"RESULT: duration backfill complete; updated={updated}; "
        f"unavailable={unavailable}; checked={checked}"
    )


def organize_recordings(token):
    moved = 0
    already_organized = 0
    unavailable = 0
    checked = 0
    after = None
    while True:
        path = "/api/internal/recordings/organize"
        if after:
            path += "?" + urllib.parse.urlencode({"after": after})
        recordings = dashboard_request(path).get("recordings", [])
        if not isinstance(recordings, list):
            raise RuntimeError("Dashboard returned an invalid recording list")
        if not recordings:
            break
        checked += len(recordings)
        for recording in recordings:
            try:
                file_id = recording["drive_id"]
                username = recording["username"]
                started_at = recording["started_at"]
                filename = recording["filename"]
                if (
                    not isinstance(file_id, str)
                    or not re.fullmatch(r"[A-Za-z0-9_-]{10,200}", file_id)
                    or not isinstance(username, str)
                    or not isinstance(started_at, str)
                    or not isinstance(filename, str)
                    or not filename
                ):
                    raise ValueError("Invalid dashboard recording metadata")
                metadata = drive_file(file_id, token)
            except APIError as exc:
                if exc.status_code == 404:
                    unavailable += 1
                    print("WARNING: a known Drive recording is no longer available")
                    continue
                raise
            if (
                metadata.get("id") != file_id
                or metadata.get("name") != filename
                or metadata.get("trashed", False)
            ):
                unavailable += 1
                print("WARNING: a Drive file did not match its dashboard recording")
                continue
            destination = recording_folder_id(username, started_at, token)
            parents = metadata.get("parents", [])
            if destination in parents:
                already_organized += 1
                continue
            parameters = {
                "addParents": destination,
                "supportsAllDrives": "true",
                "fields": "id,name,parents,trashed",
            }
            old_parents = [parent for parent in parents if parent != destination]
            if old_parents:
                parameters["removeParents"] = ",".join(old_parents)
            url = (
                f"{DRIVE_API}/files/{urllib.parse.quote(file_id, safe='')}?"
                f"{urllib.parse.urlencode(parameters)}"
            )
            status, _, raw = api_request(
                url,
                method="PATCH",
                headers={"Authorization": f"Bearer {token}"},
            )
            if status != 200:
                raise RuntimeError(f"Google Drive move failed (HTTP {status})")
            updated = json.loads(raw)
            if (
                updated.get("id") != file_id
                or updated.get("name") != filename
                or updated.get("trashed", False)
                or destination not in updated.get("parents", [])
            ):
                raise RuntimeError("Google Drive move did not pass verification")
            moved += 1
        if len(recordings) < 200:
            break
        after = recordings[-1]["drive_id"]
    print(
        "RESULT: Drive organization complete; "
        f"moved={moved}; already_organized={already_organized}; "
        f"unavailable={unavailable}; checked={checked}"
    )


def delete_recording(file_id, token):
    if not re.fullmatch(r"[A-Za-z0-9_-]{10,200}", file_id):
        raise ValueError("Invalid Google Drive file ID")
    path = "/api/internal/recordings/" + urllib.parse.quote(file_id, safe="")
    record = dashboard_request(path)
    if record.get("drive_id") != file_id or not record.get("filename"):
        raise RuntimeError("Recording metadata did not match the requested Drive file")
    try:
        metadata = drive_file(file_id, token)
    except APIError as exc:
        if exc.status_code != 404:
            raise
        print("WARNING: Google Drive file was already missing; removing its dashboard row")
    else:
        if metadata.get("id") != file_id or metadata.get("name") != record["filename"]:
            raise RuntimeError("Google Drive file did not match the dashboard recording")
        if metadata.get("trashed", False):
            print("WARNING: Google Drive file was already in the trash")
        else:
            url = f"{DRIVE_API}/files/{urllib.parse.quote(file_id, safe='')}?supportsAllDrives=true"
            status, _, _ = api_request(
                url,
                method="DELETE",
                headers={"Authorization": f"Bearer {token}"},
            )
            if status not in {200, 204}:
                raise RuntimeError(f"Google Drive deletion failed (HTTP {status})")
    dashboard_request(path, method="DELETE")
    print(f"RESULT: recording deleted from Google Drive and dashboard: {record['filename']}")


def main():
    if len(sys.argv) not in {2, 3}:
        raise RuntimeError("Usage: drive_admin.py ACTION [DRIVE_ID]")
    action = sys.argv[1]
    drive_id = sys.argv[2] if len(sys.argv) == 3 else ""
    token = refresh_token()
    if action == "backfill-durations" and not drive_id:
        backfill_durations(token)
    elif action == "organize" and not drive_id:
        organize_recordings(token)
    elif action == "delete" and drive_id:
        delete_recording(drive_id, token)
    else:
        raise ValueError("Unsupported Drive maintenance action")


if __name__ == "__main__":
    main()
