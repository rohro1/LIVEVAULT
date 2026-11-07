import json, os, sys, urllib.request

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
RANK = {"origin": 0, "uhd_60": 1, "uhd": 1, "hd_60": 2, "hd": 2, "sd": 3, "ld": 4, "md": 5}

def get_json(url):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.loads(r.read().decode("utf-8", "replace"))

def candidates(room):
    out, seen_keys = [], []
    for key, codec in (("streamData", "h264"), ("hevcStreamData", "hevc")):
        raw = ((room.get(key) or {}).get("pull_data") or {}).get("stream_data")
        if not raw:
            continue
        try:
            sd = json.loads(raw) if isinstance(raw, str) else raw
        except ValueError:
            continue
        data = sd.get("data") or {}
        seen_keys.append((codec, list(sd.keys()), list(data.keys())))
        for quality, v in data.items():
            main = (v or {}).get("main") or {}
            for fmt in ("flv", "hls"):
                if main.get(fmt):
                    out.append({"codec": codec, "quality": quality, "fmt": fmt, "url": main[fmt]})
    return out, seen_keys

def main():
    user, outfile = sys.argv[1].lstrip("@"), sys.argv[2]
    mode = os.environ.get("QUALITY", "compat")
    d = get_json(f"https://www.tiktok.com/api-live/user/room/?aid=1988&uniqueId={user}&sourceType=54")
    data = d.get("data") or {}
    status = (data.get("user") or {}).get("status")
    print(f"RESULT: user status={status} (2 means live)")
    if status != 2:
        print("RESULT: not live, nothing to record")
        sys.exit(2)
    cands, seen = candidates(data.get("liveRoom") or {})
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

main()
