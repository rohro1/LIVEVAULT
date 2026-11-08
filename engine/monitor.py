import datetime as dt
import concurrent.futures
import hashlib
import hmac
import json
import os
import re
import time
import urllib.error
import urllib.request
from zoneinfo import ZoneInfo

from tiktok import parse_live_status, request_headers, status_url

API = "https://api.github.com"
USER_AGENT = "LIVEVAULT/1.0"
ET = ZoneInfo("America/New_York")
RECORDER_RETRY_BASE_SECONDS = 5 * 60
RECORDER_RETRY_MAX_SECONDS = 5 * 60


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


def in_quiet_hours(enabled, start, end, now=None):
    enabled = enabled.lower()
    if enabled not in {"true", "false"}:
        raise ValueError("QUIET_HOURS_ENABLED must be true or false")
    if enabled == "false":
        return False
    if not re.fullmatch(r"\d{2}:\d{2}", start) or not re.fullmatch(r"\d{2}:\d{2}", end):
        raise ValueError("QUIET_START and QUIET_END must use HH:MM")
    try:
        start_time = dt.time.fromisoformat(start)
        end_time = dt.time.fromisoformat(end)
    except ValueError as exc:
        raise ValueError("QUIET_START and QUIET_END must use HH:MM") from exc
    if start_time == end_time:
        raise ValueError("QUIET_START and QUIET_END cannot be the same")
    current = (now or dt.datetime.now(ET)).astimezone(ET).time().replace(second=0, microsecond=0)
    if start_time < end_time:
        return start_time <= current < end_time
    return current >= start_time or current < end_time


def request_json(url, token=None, method="GET", body=None):
    headers = {"User-Agent": USER_AGENT, "Accept": "application/vnd.github+json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
        headers["X-GitHub-Api-Version"] = "2022-11-28"
    data = json.dumps(body).encode() if body is not None else None
    if data is not None:
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            raw = response.read()
            return json.loads(raw) if raw else None
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"GitHub API request failed (HTTP {exc.code})") from None
    except urllib.error.URLError as exc:
        raise RuntimeError(f"Network request failed ({exc.reason.__class__.__name__})") from None
    except TimeoutError:
        raise RuntimeError("GitHub API request timed out") from None


def notify_dashboard(username, online, recorder_status, message):
    endpoint = os.environ.get("DASHBOARD_API_URL", "").rstrip("/")
    token = os.environ.get("DASHBOARD_API_TOKEN", "")
    if not endpoint and not token:
        return
    if not endpoint or not token:
        raise RuntimeError("Both dashboard URL and dashboard API token are required")
    request = urllib.request.Request(
        endpoint + "/api/internal/status",
        data=json.dumps({
            "username": username,
            "online": online,
            "recorder_status": recorder_status,
            "message": message,
        }).encode(),
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    request.add_header("User-Agent", USER_AGENT)
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            if response.status not in {200, 202}:
                raise RuntimeError(f"Dashboard status update failed (HTTP {response.status})")
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"Dashboard status update failed (HTTP {exc.code})") from None
    except urllib.error.URLError as exc:
        raise RuntimeError(f"Dashboard status update failed ({exc.reason.__class__.__name__})") from None
    except TimeoutError:
        raise RuntimeError("Dashboard status update timed out") from None


def is_live(username):
    request = urllib.request.Request(status_url(username), headers=request_headers(username))
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            data = json.loads(response.read().decode("utf-8", "replace"))
    except ValueError:
        raise RuntimeError("TikTok status response was not valid JSON") from None
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"TikTok status request failed (HTTP {exc.code})") from None
    except urllib.error.URLError as exc:
        raise RuntimeError(f"TikTok status request failed ({exc.reason.__class__.__name__})") from None
    except TimeoutError:
        raise RuntimeError("TikTok status request timed out") from None
    try:
        return parse_live_status(data)
    except ValueError as exc:
        raise RuntimeError(str(exc)) from None


def check_accounts(accounts):
    if not accounts:
        return {}
    results = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=min(5, len(accounts))) as executor:
        futures = {executor.submit(check_account, username): username for username in accounts}
        for future in concurrent.futures.as_completed(futures):
            username = futures[future]
            try:
                results[username] = future.result()
            except (RuntimeError, ValueError) as exc:
                results[username] = exc
    return results


def check_account(username):
    try:
        return is_live(username)
    except RuntimeError as first_error:
        time.sleep(2)
        try:
            return is_live(username)
        except RuntimeError:
            raise first_error


def recorder_retry_delay(runs, host_key, now=None):
    account_runs = sorted(
        (
            run for run in runs
            if run.get("display_title") == f"record-{host_key}"
            and run.get("status") == "completed"
        ),
        key=lambda run: run.get("updated_at", ""),
        reverse=True,
    )
    failures = 0
    for run in account_runs:
        if run.get("conclusion") != "failure":
            break
        failures += 1
    if not failures:
        return 0

    completed_at = dt.datetime.fromisoformat(
        account_runs[0]["updated_at"].replace("Z", "+00:00")
    )
    current_time = now or dt.datetime.now(dt.timezone.utc)
    delay = min(
        RECORDER_RETRY_BASE_SECONDS * (2 ** (failures - 1)),
        RECORDER_RETRY_MAX_SECONDS,
    )
    elapsed = max(0, (current_time - completed_at).total_seconds())
    return max(0, int(delay - elapsed))


def update_dashboard_status(username, online, recorder_status, message):
    try:
        notify_dashboard(username, online, recorder_status, message)
    except RuntimeError as exc:
        print(f"WARNING: dashboard status update failed: {exc}")
        return False
    return True


def main():
    accounts = parse_accounts(os.environ.get("TRACKED", ""))
    token = os.environ.get("GITHUB_TOKEN", "")
    repo = os.environ.get("GITHUB_REPOSITORY", "")
    ref = os.environ.get("GITHUB_REF_NAME", "")
    secret = os.environ.get("HOST_KEY_SECRET", "")
    if not token or not repo or not ref:
        raise RuntimeError("GitHub Actions context is incomplete")
    if accounts and not secret:
        raise RuntimeError("HOST_KEY_SECRET is required when TRACKED is non-empty")

    for username in accounts:
        print(f"::add-mask::{username}")

    quiet_hours = in_quiet_hours(
        os.environ.get("QUIET_HOURS_ENABLED", "false"),
        os.environ.get("QUIET_START", ""),
        os.environ.get("QUIET_END", ""),
    )

    runs_url = f"{API}/repos/{repo}/actions/workflows/record.yml/runs?per_page=100"
    runs = request_json(runs_url, token=token).get("workflow_runs", [])
    active = {
        run.get("display_title", "").removeprefix("record-"): run.get("status")
        for run in runs
        if run.get("status") in {"queued", "in_progress", "waiting", "requested", "pending"}
        and run.get("display_title", "").startswith("record-")
    }

    segment_minutes = os.environ.get("SEGMENT_MINUTES", "30")
    if segment_minutes not in {"30", "60"}:
        raise ValueError("SEGMENT_MINUTES must be 30 or 60")
    started_at = dt.datetime.now(ET).strftime("%Y%m%d_%H%M%S")
    dispatched = 0
    live_count = 0
    check_failures = 0
    dispatch_failures = 0
    dashboard_failures = 0
    live_results = check_accounts(accounts)
    for username in accounts:
        key = account_key(username, secret)
        online = live_results[username]
        if isinstance(online, Exception):
            check_failures += 1
            print(f"WARNING: live status check failed for a tracked account: {online}")
            if not update_dashboard_status(
                username, None, "error", f"Live status check failed: {online}"
            ):
                dashboard_failures += 1
            continue
        if online:
            live_count += 1
        if key in active:
            if active[key] == "in_progress":
                continue
            state = "queued"
            message = (
                "Live; recorder is queued" if online
                else "Offline; queued recorder is checking the stream"
            )
        elif online and not quiet_hours:
            retry_delay = recorder_retry_delay(runs, key)
            if retry_delay:
                minutes = max(1, (retry_delay + 59) // 60)
                state = "error"
                message = (
                    "Live detected; recorder retry delayed after a recent failure "
                    f"(retry in about {minutes} minute(s))"
                )
                if not update_dashboard_status(username, True, state, message):
                    dashboard_failures += 1
                continue
            try:
                request_json(
                    f"{API}/repos/{repo}/actions/workflows/record.yml/dispatches",
                    token=token,
                    method="POST",
                    body={
                        "ref": ref,
                        "inputs": {
                            "host_key": key,
                            "first_part": "1",
                            "started_at": started_at,
                            "segment_minutes": segment_minutes,
                        },
                    },
                )
            except RuntimeError as exc:
                dispatch_failures += 1
                print(f"WARNING: recorder dispatch failed for a tracked account: {exc}")
                if not update_dashboard_status(
                    username, True, "error", f"Recorder dispatch failed: {exc}"
                ):
                    dashboard_failures += 1
                continue
            else:
                state = "queued"
                message = "Live detected; recorder queued"
                dispatched += 1
        else:
            state = "idle"
            message = (
                "Live detected; quiet hours are active" if online
                else "Account is offline"
            )
        if not update_dashboard_status(username, online, state, message):
            dashboard_failures += 1
    if quiet_hours:
        print("RESULT: quiet hours active; no new recording jobs dispatched")
    print(
        f"RESULT: live hosts={live_count}; recording jobs dispatched={dispatched}; "
        f"status check failures={check_failures}; dispatch failures={dispatch_failures}; "
        f"dashboard update failures={dashboard_failures}"
    )


if __name__ == "__main__":
    main()
