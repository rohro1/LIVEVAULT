import json
import os
import re
import sys
import urllib.parse

from record import APIError, DRIVE_API, api_request, refresh_token


def dashboard_request(path, method="GET", body=None):
    endpoint = os.environ.get("DASHBOARD_API_URL", "").rstrip("/")
    token = os.environ.get("DASHBOARD_API_TOKEN", "")
    if not endpoint or not token:
        raise RuntimeError("Dashboard integration is not configured")
    headers = {
        "Authorization": f"******",
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
        "fields": "id,name,trashed,videoMediaMetadata",
        "supportsAllDrives": "true",
    })
    url = f"{DRIVE_API}/files/{urllib.parse.quote(file_id, safe='')}?{query}"
    status, _, raw = api_request(
        url,
        headers={"Authorization": f"******"},
    )
    if status != 200:
        raise RuntimeError(f"Google Drive metadata request failed (HTTP {status})")
    return json.loads(raw)


def backfill_durations(token):
    recordings = dashboard_request("/api/internal/recordings/missing-duration").get(
        "recordings", []
    )
    updated = 0
    unavailable = 0
    for recording in recordings:
        try:
            metadata = drive_file(recording["drive_id"], token)
            duration_ms = int(
                metadata.get("videoMediaMetadata", {}).get("durationMillis", 0)
            )
            if (
                metadata.get("id") != recording["drive_id"]
                or metadata.get("name") != recording["filename"]
                or metadata.get("trashed", False)
                or duration_ms <= 0
            ):
                unavailable += 1
                print(
                    f"WARNING: duration metadata unavailable for "
                    f"{recording['filename']}"
                )
                continue
            dashboard_request(
                "/api/internal/recordings/"
                + urllib.parse.quote(recording["drive_id"], safe=""),
                method="PATCH",
                body={"duration_seconds": duration_ms / 1000},
            )
            updated += 1
        except (APIError, KeyError, TypeError, ValueError, RuntimeError) as exc:
            unavailable += 1
            print(f"WARNING: could not fetch duration for a recording ({exc})")
    print(
        f"RESULT: duration backfill complete; updated={updated}; "
        f"unavailable={unavailable}; checked={len(recordings)}"
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
                headers={"Authorization": f"******"},
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
    elif action == "delete" and drive_id:
        delete_recording(drive_id, token)
    else:
        raise ValueError("Unsupported Drive maintenance action")


if __name__ == "__main__":
    main()
