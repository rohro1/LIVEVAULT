import datetime as dt
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request

from tiktok import parse_live_status, request_headers, status_url, stream_candidates

RANK = {"origin": 0, "uhd_60": 1, "uhd": 1, "hd_60": 2, "hd": 2, "sd": 3, "ld": 4, "md": 5}
STREAM_ATTEMPTS = 6
STREAM_RETRY_SECONDS = 10

def get_json(username):
    req = urllib.request.Request(status_url(username), headers=request_headers(username))
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.loads(r.read().decode("utf-8", "replace"))

def _flags(obj):
    """Numeric/boolean values only, skipping anything that looks like an ID."""
    out = {}
    if isinstance(obj, dict):
        for k, v in obj.items():
            k = str(k)
            if isinstance(v, (bool, int)) and not re.search(r"(?:^|_)id$|Id$|ID$", k):
                out[k] = v
    return out

def _dict(value):
    return value if isinstance(value, dict) else {}

def describe_response(d):
    """Safe summary of a TikTok status response: key names and numeric/boolean flags only.
    Never includes URLs, IDs, or text values."""
    if not isinstance(d, dict):
        return {"type": type(d).__name__}
    data = _dict(d.get("data"))
    user = _dict(data.get("user"))
    room = _dict(data.get("liveRoom"))
    return {
        "top_keys": sorted(str(k) for k in d),
        "top_flags": _flags(d),
        "data_keys": sorted(str(k) for k in data),
        "user_keys": sorted(str(k) for k in user),
        "user_flags": _flags(user),
        "room_present": bool(room),
        "room_keys": sorted(str(k) for k in room),
        "room_flags": _flags(room),
    }

def main():
    user, outfile = sys.argv[1].lstrip("@"), sys.argv[2]
    roomfile = sys.argv[3] if len(sys.argv) > 3 else None
    mode = os.environ.get("QUALITY", "compat")
    pool = []
    last_error = "TikTok did not return a usable stream URL"
    for attempt in range(1, STREAM_ATTEMPTS + 1):
        checked_at = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
        print(
            f"EVENT: resolver_attempt at={checked_at} "
            f"username=@{user} attempt={attempt}/{STREAM_ATTEMPTS}"
        )
        try:
            d = get_json(user)
            data = d.get("data") or {}
            user_data = data.get("user") or {}
            status = user_data.get("status")
            if not parse_live_status(d):
                print(f"RESULT: user status={status}; no active stream detected")
                print("RESULT: not live, nothing to record")
                sys.exit(2)
            print(f"RESULT: active stream detected (user status={status})")
            if roomfile:
                with open(roomfile, "w") as f:
                    f.write(str(user_data.get("roomId") or ""))
            room = data.get("liveRoom") or {}
            cands, seen = stream_candidates(room)
            print(f"RESULT: stream data layout (key names only): {seen}")
            print(f"RESULT: found {len(cands)} stream options: " +
                  ", ".join(f"{c['codec']}/{c['quality']}/{c['fmt']}" for c in cands))
            if mode == "compat":
                pool = [c for c in cands if c["codec"] == "h264"] or cands
            else:
                pool = cands
            if pool:
                break
            print("RESULT: empty-stream diagnostics: " +
                  json.dumps(describe_response(d), sort_keys=True))
            last_error = (
                "TikTok reports this account as live but its API response "
                "contains no usable streamData/hevcStreamData URL"
            )
        except urllib.error.HTTPError as exc:
            last_error = f"TikTok stream request returned HTTP {exc.code}"
            print(
                f"RESULT: resolver request failed at={checked_at} "
                f"username=@{user} http_status={exc.code}"
            )
        except urllib.error.URLError as exc:
            last_error = (
                "TikTok stream request failed "
                f"({exc.reason.__class__.__name__})"
            )
            print(
                f"RESULT: resolver request failed at={checked_at} "
                f"username=@{user} reason={exc.reason.__class__.__name__}"
            )
        except (TimeoutError, ValueError, UnicodeError) as exc:
            last_error = (
                "TikTok stream response was invalid "
                f"({type(exc).__name__})"
            )
            print(
                f"RESULT: resolver response invalid at={checked_at} "
                f"username=@{user} reason={type(exc).__name__}"
            )
        if attempt < STREAM_ATTEMPTS:
            print(
                f"RESULT: {last_error}; retrying "
                f"({attempt}/{STREAM_ATTEMPTS - 1}) in {STREAM_RETRY_SECONDS} seconds"
            )
            time.sleep(STREAM_RETRY_SECONDS)
    if not pool:
        print(
            f"RESULT: resolver error: {last_error} after "
            f"{STREAM_ATTEMPTS} checks"
        )
        sys.exit(3)
    best = sorted(pool, key=lambda c: (RANK.get(c["quality"], 9), c["fmt"] != "flv"))[0]
    print(f"RESULT: chosen {best['codec']}/{best['quality']}/{best['fmt']}")
    with open(outfile, "w") as f:
        f.write(best["url"])

if __name__ == "__main__":
    main()
