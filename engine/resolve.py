import json
import os
import sys
import urllib.request

from tiktok import parse_live_status, request_headers, status_url, stream_candidates

RANK = {"origin": 0, "uhd_60": 1, "uhd": 1, "hd_60": 2, "hd": 2, "sd": 3, "ld": 4, "md": 5}

def get_json(username):
    req = urllib.request.Request(status_url(username), headers=request_headers(username))
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.loads(r.read().decode("utf-8", "replace"))

def main():
    user, outfile = sys.argv[1].lstrip("@"), sys.argv[2]
    roomfile = sys.argv[3] if len(sys.argv) > 3 else None
    mode = os.environ.get("QUALITY", "compat")
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
    cands, seen = stream_candidates(data.get("liveRoom") or {})
    print(f"RESULT: stream data layout (key names only): {seen}")
    print(f"RESULT: found {len(cands)} stream options: " +
          ", ".join(f"{c['codec']}/{c['quality']}/{c['fmt']}" for c in cands))
    if mode == "compat":
        pool = [c for c in cands if c["codec"] == "h264"] or cands
    else:
        pool = cands
    if not pool:
        print("RESULT: no usable stream found")
        sys.exit(3)
    best = sorted(pool, key=lambda c: (RANK.get(c["quality"], 9), c["fmt"] != "flv"))[0]
    print(f"RESULT: chosen {best['codec']}/{best['quality']}/{best['fmt']}")
    with open(outfile, "w") as f:
        f.write(best["url"])

if __name__ == "__main__":
    main()
