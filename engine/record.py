import datetime as dt
import hashlib
import hmac
import json
import os
import re
import signal
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from zoneinfo import ZoneInfo

from monitor import in_quiet_hours

API = "https://api.github.com"
GOOGLE_TOKEN = "https://oauth2.googleapis.com/token"
DRIVE_API = "https://www.googleapis.com/drive/v3"
DRIVE_UPLOAD = "https://www.googleapis.com/upload/drive/v3/files"
DRIVE_FOLDER_MIME = "application/vnd.google-apps.folder"
ET = ZoneInfo("America/New_York")
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
CHUNK_SIZE = 8 * 1024 * 1024
MAX_RUNTIME_SECONDS = 345 * 60
ACCOUNT_CHECK_INTERVAL_SECONDS = 5
_LAST_ACCOUNT_STATE = {}
_DRIVE_FOLDER_CACHE = {}


def _log_account_state(username, active, source):
    if _LAST_ACCOUNT_STATE.get(username) is active:
        return
    _LAST_ACCOUNT_STATE[username] = active
    checked_at = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
    state = "active" if active else "removed_or_inactive"
    print(
        f"EVENT: recorder_account_state at={checked_at} "
        f"username=@{username} state={state} source={source}"
    )


class NetworkError(RuntimeError):
    pass


class APIError(RuntimeError):
    def __init__(self, status_code):
        super().__init__(f"Remote API request failed (HTTP {status_code})")
        self.status_code = status_code


class CaptureStartupError(RuntimeError):
    pass


def api_request(url, method="GET", headers=None, body=None, timeout=60):
    request_headers = {"User-Agent": "LIVEVAULT/1.0"}
    request_headers.update(headers or {})
    request = urllib.request.Request(url, data=body, headers=request_headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, response.headers, response.read()
    except urllib.error.HTTPError as exc:
        if exc.code == 308:
            return exc.code, exc.headers, exc.read()
        raise APIError(exc.code) from None
    except urllib.error.URLError as exc:
        raise NetworkError(f"Network request failed ({exc.reason.__class__.__name__})") from None
    except TimeoutError:
        raise NetworkError("Network request timed out") from None


def parse_accounts(value):
    accounts = []
    seen = set()
    for item in value.split(","):
        user = item.strip().lstrip("@").lower()
        if not user:
            continue
        if not re.fullmatch(r"[a-z0-9._]{1,24}", user):
            raise ValueError("TRACKED contains an invalid TikTok username")
        if user not in seen:
            seen.add(user)
            accounts.append(user)
    return accounts


def account_key(username, secret):
    return hmac.new(secret.encode(), username.encode(), hashlib.sha256).hexdigest()


def tracked_user(host_key, tracked, secret):
    for username in parse_accounts(tracked):
        if hmac.compare_digest(account_key(username, secret), host_key):
            return username
    return None


def github_request(repo, token, method="GET", body=None):
    url = f"{API}/repos/{repo}/actions/variables/TRACKED"
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    data = json.dumps(body).encode() if body is not None else None
    if data is not None:
        headers["Content-Type"] = "application/json"
    status, _, raw = api_request(url, method=method, headers=headers, body=data)
    if status != 200:
        raise RuntimeError(f"Unable to read TRACKED repository variable (HTTP {status})")
    return json.loads(raw).get("value", "")


def refresh_tracked_user(host_key, repo, token, secret):
    tracked = os.environ.get("TRACKED")
    if tracked is None:
        tracked = github_request(repo, token)
    username = tracked_user(host_key, tracked, secret)
    if not username:
        return None

    endpoint = os.environ.get("DASHBOARD_API_URL", "").rstrip("/")
    dashboard_token = os.environ.get("DASHBOARD_API_TOKEN", "")
    if not endpoint or not dashboard_token:
        raise RuntimeError("Dashboard integration is not configured for recorder checks")
    url = (
        endpoint
        + "/api/internal/accounts/"
        + urllib.parse.quote(username, safe="")
    )
    status, _, raw = api_request(
        url,
        headers={"Authorization": f"Bearer {dashboard_token}"},
        timeout=20,
    )
    if status != 200:
        raise RuntimeError(f"Dashboard account check failed (HTTP {status})")
    try:
        active = json.loads(raw).get("active") is True
    except (AttributeError, TypeError, ValueError):
        raise RuntimeError("Dashboard account check returned an invalid response") from None
    _log_account_state(username, active, "dashboard")
    if not active:
        return None
    return username


def refresh_token():
    required = ("DRIVE_CLIENT_ID", "DRIVE_CLIENT_SECRET", "DRIVE_REFRESH_TOKEN")
    missing = [key for key in required if not os.environ.get(key)]
    if missing:
        raise RuntimeError("Google Drive OAuth secrets are missing: " + ", ".join(missing))
    payload = urllib.parse.urlencode({
        "client_id": os.environ["DRIVE_CLIENT_ID"],
        "client_secret": os.environ["DRIVE_CLIENT_SECRET"],
        "refresh_token": os.environ["DRIVE_REFRESH_TOKEN"],
        "grant_type": "refresh_token",
    }).encode()
    status, _, raw = api_request(
        GOOGLE_TOKEN,
        method="POST",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        body=payload,
    )
    if status != 200:
        raise RuntimeError(f"Google OAuth refresh failed (HTTP {status})")
    data = json.loads(raw)
    access_token = data.get("access_token")
    if not access_token:
        raise RuntimeError("Google OAuth response did not contain an access token")
    return access_token


def ensure_drive_folder(name, token, parent_id=None):
    if not name or "/" in name or "\\" in name:
        raise ValueError("Invalid Google Drive folder name")
    if parent_id and not re.fullmatch(r"[A-Za-z0-9_-]+", parent_id):
        raise ValueError("Invalid Google Drive parent folder ID")
    escaped_name = name.replace("\\", "\\\\").replace("'", "\\'")
    query = (
        f"name = '{escaped_name}' and mimeType = '{DRIVE_FOLDER_MIME}' "
        "and trashed = false"
    )
    if parent_id:
        query += f" and '{parent_id}' in parents"
    else:
        query += " and 'root' in parents"
    page_token = None
    folders = []
    while True:
        parameters = {
            "q": query,
            "spaces": "drive",
            "pageSize": "100",
            "fields": "nextPageToken,files(id,name,mimeType,parents,createdTime)",
            "supportsAllDrives": "true",
            "includeItemsFromAllDrives": "true",
        }
        if page_token:
            parameters["pageToken"] = page_token
        url = f"{DRIVE_API}/files?{urllib.parse.urlencode(parameters)}"
        status, _, raw = api_request(
            url,
            headers={"Authorization": f"Bearer {token}"},
        )
        if status != 200:
            raise RuntimeError(f"Google Drive folder lookup failed (HTTP {status})")
        result = json.loads(raw)
        folders.extend(result.get("files", []))
        page_token = result.get("nextPageToken")
        if not page_token:
            break
    if len(folders) > 1:
        folders.sort(
            key=lambda folder: (
                folder.get("createdTime", ""),
                folder.get("id", ""),
            )
        )
    if folders:
        folder = folders[0]
        if folder.get("name") != name or folder.get("mimeType") != DRIVE_FOLDER_MIME:
            raise RuntimeError("Google Drive returned an unexpected folder")
        return folder["id"]

    metadata = {"name": name, "mimeType": DRIVE_FOLDER_MIME}
    if parent_id:
        metadata["parents"] = [parent_id]
    status, _, raw = api_request(
        f"{DRIVE_API}/files",
        method="POST",
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
        body=json.dumps(metadata).encode(),
    )
    if status not in {200, 201}:
        raise RuntimeError(f"Google Drive folder creation failed (HTTP {status})")
    folder = json.loads(raw)
    if (
        not folder.get("id")
        or folder.get("name") != name
        or folder.get("mimeType") != DRIVE_FOLDER_MIME
    ):
        raise RuntimeError("Google Drive did not verify the created folder")
    return folder["id"]


def recording_folder_id(username, stamp, token):
    if not re.fullmatch(r"[a-z0-9._]{1,24}", username):
        raise ValueError("Invalid TikTok username for Google Drive folder")
    if not re.fullmatch(r"\d{8}_\d{6}", stamp):
        raise ValueError("Invalid recording timestamp for Google Drive folder")
    try:
        dt.datetime.strptime(stamp, "%Y%m%d_%H%M%S")
    except ValueError:
        raise ValueError("Invalid recording timestamp for Google Drive folder") from None
    root_id = _DRIVE_FOLDER_CACHE.get(("LIVEVAULT", None))
    if not root_id:
        root_id = ensure_drive_folder("LIVEVAULT", token)
        _DRIVE_FOLDER_CACHE[("LIVEVAULT", None)] = root_id
    user_key = (username, root_id)
    user_id = _DRIVE_FOLDER_CACHE.get(user_key)
    if not user_id:
        user_id = ensure_drive_folder(username, token, root_id)
        _DRIVE_FOLDER_CACHE[user_key] = user_id
    date = f"{stamp[:4]}-{stamp[4:6]}-{stamp[6:8]}"
    date_key = (date, user_id)
    date_id = _DRIVE_FOLDER_CACHE.get(date_key)
    if not date_id:
        date_id = ensure_drive_folder(date, token, user_id)
        _DRIVE_FOLDER_CACHE[date_key] = date_id
    return date_id


def _upload_state(session_url, token, total):
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Length": "0",
        "Content-Range": f"bytes */{total}",
    }
    try:
        status, response_headers, _ = api_request(
            session_url, method="PUT", headers=headers, body=b"", timeout=60
        )
    except RuntimeError as exc:
        if "HTTP 404" in str(exc):
            raise RuntimeError("Google Drive upload session expired") from None
        raise
    if status in {200, 201}:
        return total, json.loads(_)
    if status != 308:
        raise RuntimeError(f"Google Drive upload status check returned HTTP {status}")
    match = re.fullmatch(r"bytes=0-(\d+)", response_headers.get("Range", ""))
    return (int(match.group(1)) + 1 if match else 0), None


def drive_upload(path, filename, username, stamp):
    total = path.stat().st_size
    if total <= 0:
        raise RuntimeError("Refusing to upload an empty recording part")
    access_token = refresh_token()
    folder_id = recording_folder_id(username, stamp, access_token)
    metadata = json.dumps({
        "name": filename,
        "mimeType": "video/mp4",
        "parents": [folder_id],
    }).encode()
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json; charset=UTF-8",
        "X-Upload-Content-Type": "video/mp4",
        "X-Upload-Content-Length": str(total),
    }
    status, response_headers, _ = api_request(
        DRIVE_UPLOAD + "?uploadType=resumable",
        method="POST",
        headers=headers,
        body=metadata,
    )
    if status not in {200, 201} or not response_headers.get("Location"):
        raise RuntimeError("Google Drive did not create an upload session")
    session_url = response_headers["Location"]
    offset = 0
    upload_result = None
    with path.open("rb") as stream:
        while offset < total and upload_result is None:
            stream.seek(offset)
            chunk = stream.read(min(CHUNK_SIZE, total - offset))
            if not chunk:
                raise RuntimeError("Recording file ended before its reported size")
            end = offset + len(chunk) - 1
            chunk_headers = {
                "Authorization": f"Bearer {access_token}",
                "Content-Type": "video/mp4",
                "Content-Length": str(len(chunk)),
                "Content-Range": f"bytes {offset}-{end}/{total}",
            }
            response_received = False
            for attempt in range(5):
                try:
                    status, response_headers, raw = api_request(
                        session_url, method="PUT", headers=chunk_headers, body=chunk, timeout=180
                    )
                    response_received = True
                    break
                except (NetworkError, APIError) as exc:
                    if isinstance(exc, APIError) and exc.status_code not in {
                        408, 429, 500, 502, 503, 504,
                    }:
                        raise
                    if attempt == 4:
                        raise RuntimeError(
                            "Google Drive upload chunk failed after 5 attempts: "
                            f"{exc}"
                        ) from exc
                    try:
                        reported_offset, completed = _upload_state(
                            session_url, access_token, total
                        )
                    except (NetworkError, APIError) as state_error:
                        if isinstance(state_error, APIError) and state_error.status_code not in {
                            408, 429, 500, 502, 503, 504,
                        }:
                            raise
                        time.sleep(min(2 ** attempt, 8))
                        continue
                    if completed:
                        upload_result = completed
                        break
                    if reported_offset != offset:
                        offset = reported_offset
                        break
                    time.sleep(min(2 ** attempt, 8))
            if upload_result:
                break
            if not response_received:
                continue
            if status in {200, 201}:
                upload_result = json.loads(raw)
                continue
            if status != 308:
                raise RuntimeError(f"Google Drive upload returned HTTP {status}")
            match = re.fullmatch(r"bytes=0-(\d+)", response_headers.get("Range", ""))
            new_offset = int(match.group(1)) + 1 if match else offset
            if new_offset <= offset:
                raise RuntimeError("Google Drive upload made no progress")
            offset = new_offset
    if not upload_result:
        raise RuntimeError("Google Drive upload ended without a completion response")

    file_id = upload_result.get("id")
    if not file_id:
        raise RuntimeError("Google Drive upload response did not include a file ID")
    verify_fields = urllib.parse.quote(
        "id,name,size,webViewLink,trashed,parents",
        safe=",",
    )
    verify_url = (
        f"{DRIVE_API}/files/{urllib.parse.quote(file_id, safe='')}"
        f"?fields={verify_fields}"
    )
    verify_headers = {"Authorization": f"Bearer {access_token}"}
    verify_status, _, raw = api_request(verify_url, headers=verify_headers)
    if verify_status != 200:
        raise RuntimeError(f"Google Drive verification failed (HTTP {verify_status})")
    verified = json.loads(raw)
    if (
        verified.get("id") != file_id
        or verified.get("name") != filename
        or int(verified.get("size", -1)) != total
        or verified.get("trashed", False)
        or not verified.get("webViewLink")
        or folder_id not in verified.get("parents", [])
    ):
        raise RuntimeError("Google Drive file did not pass post-upload verification")
    return verified


def notify_dashboard(username, room_id, stamp, part, filename, duration_seconds, verified):
    endpoint = os.environ.get("DASHBOARD_API_URL", "").rstrip("/")
    token = os.environ.get("DASHBOARD_API_TOKEN", "")
    if not endpoint and not token:
        return
    if not endpoint or not token:
        raise RuntimeError("Both dashboard recording-ingest secrets must be configured")
    print(f"::add-mask::{token}")
    payload = {
        "username": username,
        "room_id": room_id,
        "started_at": stamp,
        "part_number": part,
        "filename": filename,
        "drive_id": verified["id"],
        "drive_url": verified["webViewLink"],
        "file_size": int(verified["size"]),
        "duration_seconds": duration_seconds,
    }
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }
    status, _, _ = api_request(
        endpoint + "/api/internal/recordings",
        method="POST",
        headers=headers,
        body=json.dumps(payload).encode(),
    )
    if status not in {200, 201}:
        raise RuntimeError(f"Dashboard recording update failed (HTTP {status})")


def notify_dashboard_status(username, online, recorder_status, message):
    endpoint = os.environ.get("DASHBOARD_API_URL", "").rstrip("/")
    token = os.environ.get("DASHBOARD_API_TOKEN", "")
    if not endpoint and not token:
        return
    if not endpoint or not token:
        raise RuntimeError("Both dashboard URL and dashboard API token are required")
    print(f"::add-mask::{token}")
    payload = {
        "username": username,
        "online": online,
        "recorder_status": recorder_status,
        "message": message,
    }
    status, _, _ = api_request(
        endpoint + "/api/internal/status",
        method="POST",
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
        body=json.dumps(payload).encode(),
    )
    if status not in {200, 202}:
        raise RuntimeError(f"Dashboard status update failed (HTTP {status})")


def resolve_stream(username, directory):
    url_file = directory / "stream.url"
    room_file = directory / "room.id"
    result = subprocess.run(
        [sys.executable, "engine/resolve.py", username, str(url_file), str(room_file)],
        capture_output=True,
        text=True,
        check=False,
    )
    for line in result.stdout.splitlines():
        print(line)
    if result.returncode == 2:
        return None
    if result.returncode != 0:
        failure = next(
            (
                line.removeprefix("RESULT: resolver error: ")
                for line in result.stdout.splitlines()
                if line.startswith("RESULT: resolver error: ")
            ),
            "Stream resolver failed; see the preceding RESULT lines",
        )
        raise RuntimeError(failure)
    stream_url = url_file.read_text().strip()
    room_id = room_file.read_text().strip()
    if not stream_url or not room_id:
        raise RuntimeError("Stream resolver returned incomplete room data")
    print(f"::add-mask::{stream_url}")
    return stream_url, room_id


def probe_duration(path):
    result = subprocess.run(
        [
            "ffprobe", "-v", "error", "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1", str(path),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError("FFprobe could not inspect a recording segment")
    try:
        duration = float(result.stdout.strip())
    except ValueError:
        raise RuntimeError("FFprobe returned an invalid duration") from None
    if duration <= 0:
        raise RuntimeError("Recording segment has no duration")
    return duration


def capture_has_data(directory):
    return any(
        path.is_file() and path.stat().st_size > 0
        for path in directory.glob("part_*.ts")
    )


def ffmpeg_error_summary(path):
    details = path.read_text(errors="replace").strip()
    details = re.sub(r"https?://\S+", "[URL]", details)
    details = " ".join(details.split())
    return details[-1000:] or "no diagnostic output"


def save_failure_reason(error):
    env_file = os.environ.get("GITHUB_ENV")
    if not env_file:
        return
    details = re.sub(r"https?://\S+", "[URL]", str(error))
    details = " ".join(details.split())[:150] or type(error).__name__
    with open(env_file, "a", encoding="utf-8") as stream:
        stream.write(f"LIVEVAULT_FAILURE_REASON={details}\n")


def upload_segments(directory, username, room_id, stamp, next_part, finalized):
    pattern = re.compile(r"part_(\d+)\.ts$")
    segments = sorted(
        (path for path in directory.glob("part_*.ts") if pattern.fullmatch(path.name)),
        key=lambda path: int(pattern.fullmatch(path.name).group(1)),
    )
    if not finalized and segments:
        segments = segments[:-1]
    failures = []
    for segment in segments:
        part = int(pattern.fullmatch(segment.name).group(1))
        mp4 = segment.with_suffix(".mp4")
        try:
            result = subprocess.run(
                [
                    "ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin",
                    "-i", str(segment), "-c", "copy", "-movflags", "+faststart", str(mp4),
                ],
                capture_output=True,
                text=True,
                check=False,
            )
            if not mp4.is_file():
                raise RuntimeError(f"Could not convert recording part {part} to MP4")
            duration_seconds = probe_duration(mp4)
            if result.returncode != 0:
                print(f"RESULT: salvaging playable partial MP4 for part {part:03d}")
            filename = f"{username}_{room_id}_{stamp}_part{part:03d}.mp4"
            verified = drive_upload(mp4, filename, username, stamp)
            notify_dashboard(
                username, room_id, stamp, part, filename, duration_seconds, verified
            )
            mp4.unlink()
            segment.unlink()
            print(f"RESULT: part {part:03d} uploaded and verified in Google Drive")
            next_part = max(next_part, part + 1)
        except (OSError, RuntimeError, ValueError, subprocess.SubprocessError) as exc:
            failures.append(f"part {part:03d}: {exc}")
    if failures:
        raise RuntimeError(
            "One or more recording parts could not be uploaded: " + "; ".join(failures)
        )
    return next_part


def run_capture(
    stream_url, room_id, username, stamp, segment_minutes, next_part, deadline,
    host_key, repo, token, secret,
):
    with tempfile.TemporaryDirectory(prefix="livevault-") as temp_dir:
        directory = Path(temp_dir)
        remaining = max(1, int(deadline - time.monotonic()))
        command = [
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin",
            "-user_agent", UA, "-rw_timeout", "15000000", "-i", stream_url,
            "-t", str(remaining), "-c", "copy", "-f", "segment",
            "-segment_time", str(segment_minutes * 60),
            "-segment_start_number", str(next_part),
            "-segment_format", "mpegts", "-reset_timestamps", "1",
            str(directory / "part_%03d.ts"),
        ]
        stop_reason = None
        last_account_check = 0
        last_dashboard_update = 0
        capture_started = False
        started_wait_deadline = time.monotonic() + 60
        with (directory / "ffmpeg.log").open("w") as log:
            process = subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=log)
            capture_error = None
            try:
                while process.poll() is None:
                    now = time.monotonic()
                    if not capture_started and capture_has_data(directory):
                        capture_started = True
                        notify_dashboard_status(
                            username, True, "recording", "Recording stream"
                        )
                        last_dashboard_update = now
                    elif capture_started and now - last_dashboard_update >= 60:
                        notify_dashboard_status(
                            username, True, "recording", "Recording stream"
                        )
                        last_dashboard_update = now
                    elif not capture_started and now >= started_wait_deadline:
                        raise CaptureStartupError(
                            "FFmpeg did not capture stream data within 60 seconds: "
                            + ffmpeg_error_summary(directory / "ffmpeg.log")
                        )
                    if now - last_account_check >= ACCOUNT_CHECK_INTERVAL_SECONDS:
                        last_account_check = now
                        if not refresh_tracked_user(host_key, repo, token, secret):
                            stop_reason = "untracked"
                        elif in_quiet_hours(
                            os.environ.get("QUIET_HOURS_ENABLED", "false"),
                            os.environ.get("QUIET_START", ""),
                            os.environ.get("QUIET_END", ""),
                        ):
                            stop_reason = "quiet"
                    if stop_reason:
                        break
                    next_part = upload_segments(
                        directory, username, room_id, stamp, next_part, finalized=False
                    )
                    time.sleep(ACCOUNT_CHECK_INTERVAL_SECONDS)
            except Exception as exc:
                capture_error = exc
            finally:
                if process.poll() is None:
                    process.send_signal(signal.SIGINT)
                    try:
                        process.wait(timeout=30)
                    except subprocess.TimeoutExpired:
                        process.terminate()
                        process.wait(timeout=10)
            try:
                next_part = upload_segments(
                    directory, username, room_id, stamp, next_part, finalized=True
                )
            except Exception as upload_error:
                if capture_error:
                    raise RuntimeError(
                        f"Capture failed ({capture_error}); final part upload also failed "
                        f"({upload_error})"
                    ) from capture_error
                raise
            if capture_error:
                raise capture_error
        if process.returncode not in {0, 255}:
            if not capture_started:
                raise CaptureStartupError(
                    "FFmpeg exited before capturing stream data: "
                    + ffmpeg_error_summary(directory / "ffmpeg.log")
                )
            print("RESULT: stream ended or reset; checking whether it is still live")
        return next_part, stop_reason


def dispatch_handoff(repo, token, ref, host_key, first_part, stamp, segment_minutes):
    payload = {
        "ref": ref,
        "inputs": {
            "host_key": host_key,
            "first_part": str(first_part),
            "started_at": stamp,
            "segment_minutes": str(segment_minutes),
        },
    }
    url = f"{API}/repos/{repo}/actions/workflows/record.yml/dispatches"
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "Content-Type": "application/json",
    }
    status, _, _ = api_request(url, method="POST", headers=headers, body=json.dumps(payload).encode())
    if status != 204:
        raise RuntimeError(f"Recording handoff dispatch failed (HTTP {status})")
    print("RESULT: next recording job dispatched")


def main():
    if len(sys.argv) != 5:
        raise RuntimeError("Usage: record.py HOST_KEY FIRST_PART STARTED_AT SEGMENT_MINUTES")
    host_key, first_part_text, stamp, segment_text = sys.argv[1:]
    if not re.fullmatch(r"[0-9a-f]{64}", host_key):
        raise ValueError("Invalid host key")
    if not re.fullmatch(r"\d{8}_\d{6}", stamp):
        raise ValueError("Invalid recording start time")
    next_part = int(first_part_text)
    segment_minutes = int(segment_text)
    if next_part < 1 or segment_minutes not in {30, 60}:
        raise ValueError("Part number or segment duration is invalid")

    repo = os.environ.get("GITHUB_REPOSITORY", "")
    token = os.environ.get("GITHUB_TOKEN", "")
    secret = os.environ.get("HOST_KEY_SECRET", "")
    ref = os.environ.get("GITHUB_REF_NAME", "")
    if not all((repo, token, secret, ref)):
        raise RuntimeError("GitHub Actions context is incomplete")
    print(f"::add-mask::{host_key}")

    started = time.monotonic()
    deadline = started + MAX_RUNTIME_SECONDS
    username = refresh_tracked_user(host_key, repo, token, secret)
    if not username:
        print("RESULT: account is no longer tracked; recording skipped")
        return
    if in_quiet_hours(
        os.environ.get("QUIET_HOURS_ENABLED", "false"),
        os.environ.get("QUIET_START", ""),
        os.environ.get("QUIET_END", ""),
    ):
        print("RESULT: quiet hours active; recording skipped")
        return
    print("RESULT: recording worker started")

    with tempfile.TemporaryDirectory(prefix="livevault-resolve-") as temp_dir:
        directory = Path(temp_dir)
        while time.monotonic() < deadline:
            username = refresh_tracked_user(host_key, repo, token, secret)
            if not username:
                print("RESULT: account is no longer tracked; stopping")
                return
            resolved = resolve_stream(username, directory)
            if not resolved:
                notify_dashboard_status(
                    username, False, "idle", "Account is offline; recording stopped"
                )
                print("RESULT: account is offline; recording stopped")
                return
            stream_url, room_id = resolved
            notify_dashboard_status(
                username, True, "queued", "Stream found; waiting for recording data"
            )
            try:
                next_part, stop_reason = run_capture(
                    stream_url, room_id, username, stamp, segment_minutes, next_part, deadline,
                    host_key, repo, token, secret,
                )
            except CaptureStartupError as exc:
                print(f"WARNING: {exc}")
                notify_dashboard_status(
                    username,
                    True,
                    "queued",
                    "Stream connection failed before recording data; retrying shortly",
                )
                time.sleep(min(15, max(0, deadline - time.monotonic())))
                continue
            if stop_reason == "untracked":
                notify_dashboard_status(
                    username, False, "idle", "Account was removed; recording stopped"
                )
                print("RESULT: account is no longer tracked; recording stopped")
                return
            if stop_reason == "quiet":
                notify_dashboard_status(
                    username, True, "idle", "Quiet hours started; recording stopped"
                )
                print("RESULT: quiet hours started; recording stopped")
                return
            if time.monotonic() >= deadline:
                break
            username = refresh_tracked_user(host_key, repo, token, secret)
            if not username:
                print("RESULT: account is no longer tracked; stopping")
                return
            if not resolve_stream(username, directory):
                notify_dashboard_status(
                    username, False, "idle", "Account went offline; recording stopped"
                )
                print("RESULT: account is offline; recording stopped")
                return
            print("RESULT: stream reconnecting")
            time.sleep(15)

    username = refresh_tracked_user(host_key, repo, token, secret)
    if not username:
        print("RESULT: account is no longer tracked; handoff skipped")
        return
    if in_quiet_hours(
        os.environ.get("QUIET_HOURS_ENABLED", "false"),
        os.environ.get("QUIET_START", ""),
        os.environ.get("QUIET_END", ""),
    ):
        print("RESULT: quiet hours active; handoff skipped")
        return
    with tempfile.TemporaryDirectory(prefix="livevault-handoff-") as temp_dir:
        if not resolve_stream(username, Path(temp_dir)):
            print("RESULT: account is offline; handoff skipped")
            return
    dispatch_handoff(repo, token, ref, host_key, next_part, stamp, segment_minutes)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        save_failure_reason(exc)
        raise
