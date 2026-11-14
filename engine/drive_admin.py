import datetime
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
    DRIVE_FOLDER_MIME,
    api_request,
    drive_children,
    drive_folders,
    ensure_drive_folder,
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
        "fields": "id,name,mimeType,trashed,parents,videoMediaMetadata",
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


def trash_empty_folder(folder, name, parent_id, token):
    folder_id = folder.get("id")
    if (
        not isinstance(folder_id, str)
        or not re.fullmatch(r"[A-Za-z0-9_-]+", folder_id)
        or folder.get("name") != name
        or folder.get("mimeType") != DRIVE_FOLDER_MIME
        or folder.get("trashed", False)
        or parent_id not in folder.get("parents", [])
    ):
        raise RuntimeError("Refusing to remove an unverified Drive folder")
    if drive_children(folder_id, token):
        return False
    query = urllib.parse.urlencode({
        "supportsAllDrives": "true",
        "fields": "id,name,mimeType,parents,trashed",
    })
    url = f"{DRIVE_API}/files/{urllib.parse.quote(folder_id, safe='')}?{query}"
    status, _, raw = api_request(
        url,
        method="PATCH",
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
        body=json.dumps({"trashed": True}).encode(),
    )
    if status != 200:
        raise RuntimeError(f"Google Drive duplicate folder cleanup failed (HTTP {status})")
    updated = json.loads(raw)
    if (
        updated.get("id") != folder_id
        or updated.get("name") != name
        or updated.get("mimeType") != DRIVE_FOLDER_MIME
        or parent_id not in updated.get("parents", [])
        or updated.get("trashed") is not True
    ):
        raise RuntimeError("Google Drive duplicate folder cleanup was not verified")
    return True


def move_drive_item(item, source_id, destination_id, token):
    item_id = item.get("id")
    parents = item.get("parents")
    if (
        not isinstance(item_id, str)
        or not re.fullmatch(r"[A-Za-z0-9_-]+", item_id)
        or not isinstance(item.get("name"), str)
        or not item.get("name")
        or item.get("trashed", False)
        or not isinstance(parents, list)
        or not all(isinstance(parent, str) for parent in parents)
        or not isinstance(source_id, str)
        or not re.fullmatch(r"[A-Za-z0-9_-]+", source_id)
        or source_id not in parents
        or not isinstance(destination_id, str)
        or not re.fullmatch(r"[A-Za-z0-9_-]+", destination_id)
        or source_id == destination_id
    ):
        raise RuntimeError("Refusing to move an unverified Drive item")
    parameters = {
        "supportsAllDrives": "true",
        "fields": "id,name,mimeType,parents,trashed",
    }
    if destination_id not in parents:
        parameters["addParents"] = destination_id
    parameters["removeParents"] = source_id
    url = (
        f"{DRIVE_API}/files/{urllib.parse.quote(item_id, safe='')}?"
        f"{urllib.parse.urlencode(parameters)}"
    )
    status, _, raw = api_request(
        url,
        method="PATCH",
        headers={"Authorization": f"Bearer {token}"},
    )
    if status != 200:
        raise RuntimeError(f"Google Drive item move failed (HTTP {status})")
    updated = json.loads(raw)
    if (
        updated.get("id") != item_id
        or updated.get("name") != item["name"]
        or updated.get("trashed", False)
        or source_id in updated.get("parents", [])
        or destination_id not in updated.get("parents", [])
    ):
        raise RuntimeError("Google Drive item move did not pass verification")


def is_date_folder(name):
    if not isinstance(name, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", name):
        return False
    try:
        datetime.datetime.strptime(name, "%Y-%m-%d")
    except ValueError:
        return False
    return True


def consolidate_drive_folders(token, dashboard_usernames, recording_usernames):
    roots = drive_folders("LIVEVAULT", None, token)
    if not roots:
        ensure_drive_folder("LIVEVAULT", token)
        roots = drive_folders("LIVEVAULT", None, token)
        if not roots:
            raise RuntimeError("Google Drive root folder creation failed")
    root_id = roots[0]["id"]
    if not isinstance(root_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]+", root_id):
        raise RuntimeError("Google Drive returned an invalid LIVEVAULT folder")

    folders_by_root = {}
    usernames = set(dashboard_usernames)
    retained_usernames = usernames | set(recording_usernames)
    usernames.update(recording_usernames)
    for root in roots:
        root_folder_id = root.get("id")
        if not isinstance(root_folder_id, str):
            raise RuntimeError("Google Drive returned an invalid LIVEVAULT folder")
        children = drive_children(root_folder_id, token)
        user_folders = [
            item for item in children
            if item.get("mimeType") == DRIVE_FOLDER_MIME
            and isinstance(item.get("name"), str)
            and re.fullmatch(r"[a-z0-9._]{1,24}", item["name"])
        ]
        folders_by_root[root_folder_id] = user_folders
        usernames.update(item["name"] for item in user_folders)

    duplicate_folders_trashed = 0
    duplicate_folders_retained = 0
    orphan_folders_trashed = 0
    orphan_folders_retained = 0
    for username in sorted(usernames):
        if not re.fullmatch(r"[a-z0-9._]{1,24}", username):
            raise ValueError("Dashboard returned an invalid TikTok username")
        canonical_user_id = ensure_drive_folder(username, token, root_id)
        user_folders = [
            folder
            for root_folders in folders_by_root.values()
            for folder in root_folders
            if folder.get("name") == username
        ]
        if not any(folder.get("id") == canonical_user_id for folder in user_folders):
            canonical_user = next(
                (folder for folder in drive_children(root_id, token)
                 if folder.get("id") == canonical_user_id),
                None,
            )
            if not canonical_user:
                raise RuntimeError("Google Drive canonical account folder was not found")
            user_folders.append(canonical_user)
        date_folders = [
            (user_folder, date_folder)
            for user_folder in user_folders
            for date_folder in drive_children(user_folder["id"], token)
            if date_folder.get("mimeType") == DRIVE_FOLDER_MIME
            and is_date_folder(date_folder.get("name"))
        ]
        date_names = sorted({folder["name"] for _, folder in date_folders})
        for date_name in date_names:
            canonical_date_id = ensure_drive_folder(
                date_name, token, canonical_user_id
            )
            matching_dates = [
                folder for _, folder in date_folders
                if folder.get("name") == date_name
            ]
            for duplicate in matching_dates:
                duplicate_id = duplicate.get("id")
                if duplicate_id == canonical_date_id:
                    continue
                for child in drive_children(duplicate_id, token):
                    move_drive_item(child, duplicate_id, canonical_date_id, token)
                if trash_empty_folder(
                    duplicate, date_name, duplicate.get("parents", [None])[0], token
                ):
                    duplicate_folders_trashed += 1
                else:
                    duplicate_folders_retained += 1

        for duplicate_user in user_folders:
            duplicate_user_id = duplicate_user.get("id")
            if duplicate_user_id == canonical_user_id:
                continue
            if trash_empty_folder(
                duplicate_user,
                username,
                duplicate_user.get("parents", [None])[0],
                token,
            ):
                duplicate_folders_trashed += 1
            else:
                duplicate_folders_retained += 1

        if username not in retained_usernames:
            canonical_user = next(
                (folder for folder in drive_children(root_id, token)
                 if folder.get("id") == canonical_user_id),
                None,
            )
            if not canonical_user:
                raise RuntimeError("Google Drive canonical account folder was not found")
            for date_folder in drive_children(canonical_user_id, token):
                if (
                    date_folder.get("mimeType") == DRIVE_FOLDER_MIME
                    and is_date_folder(date_folder.get("name"))
                ):
                    if trash_empty_folder(
                        date_folder,
                        date_folder["name"],
                        canonical_user_id,
                        token,
                    ):
                        orphan_folders_trashed += 1
                    else:
                        orphan_folders_retained += 1
            if trash_empty_folder(canonical_user, username, root_id, token):
                orphan_folders_trashed += 1
            else:
                orphan_folders_retained += 1

    for duplicate_root in roots:
        if duplicate_root.get("id") == root_id:
            continue
        if trash_empty_folder(
            duplicate_root,
            "LIVEVAULT",
            "root",
            token,
        ):
            duplicate_folders_trashed += 1
        else:
            duplicate_folders_retained += 1

    return (
        duplicate_folders_trashed,
        duplicate_folders_retained,
        orphan_folders_trashed,
        orphan_folders_retained,
    )


def organize_recordings(token):
    moved = 0
    already_organized = 0
    unavailable = 0
    checked = 0
    duplicate_folders_trashed = 0
    duplicate_folders_retained = 0
    orphan_folders_trashed = 0
    orphan_folders_retained = 0
    recording_usernames = set()
    folder_cache = {}
    account_response = dashboard_request("/api/internal/accounts")
    dashboard_usernames = account_response.get("usernames", [])
    if (
        not isinstance(dashboard_usernames, list)
        or any(
            not isinstance(username, str)
            or not re.fullmatch(r"[a-z0-9._]{1,24}", username)
            for username in dashboard_usernames
        )
    ):
        raise RuntimeError("Dashboard returned an invalid account list")
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
            destination = recording_folder_id(
                username, started_at, token, folder_cache
            )
            recording_usernames.add(username)
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
    (
        duplicate_folders_trashed,
        duplicate_folders_retained,
        orphan_folders_trashed,
        orphan_folders_retained,
    ) = (
        consolidate_drive_folders(
            token,
            dashboard_usernames,
            sorted(recording_usernames),
        )
    )
    if duplicate_folders_retained or orphan_folders_retained:
        print(
            "WARNING: Drive folders with remaining contents were "
            "preserved for manual review"
        )
    print(
        "RESULT: Drive organization complete; "
        f"moved={moved}; already_organized={already_organized}; "
        f"unavailable={unavailable}; checked={checked}; "
        f"duplicate_folders_trashed={duplicate_folders_trashed}; "
        f"duplicate_folders_retained={duplicate_folders_retained}; "
        f"empty_orphan_folders_trashed={orphan_folders_trashed}; "
        f"orphan_folders_retained={orphan_folders_retained}"
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
