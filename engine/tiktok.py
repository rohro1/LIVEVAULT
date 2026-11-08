import json
from urllib.parse import quote


USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)


def status_url(username):
    return (
        "https://www.tiktok.com/api-live/user/room/?aid=1988&uniqueId="
        f"{quote(username, safe='')}&sourceType=54"
    )


def request_headers(username):
    return {
        "User-Agent": USER_AGENT,
        "Accept": "application/json, text/plain, */*",
        "Referer": f"https://www.tiktok.com/@{quote(username, safe='')}/live",
    }


def stream_candidates(room):
    out, seen_keys = [], []
    if not isinstance(room, dict):
        return out, seen_keys
    for key, codec in (("streamData", "h264"), ("hevcStreamData", "hevc")):
        stream = room.get(key)
        pull_data = stream.get("pull_data") if isinstance(stream, dict) else None
        raw = pull_data.get("stream_data") if isinstance(pull_data, dict) else None
        if not raw:
            continue
        try:
            stream_data = json.loads(raw) if isinstance(raw, str) else raw
        except (TypeError, ValueError):
            continue
        if not isinstance(stream_data, dict):
            continue
        data = stream_data.get("data") or {}
        if not isinstance(data, dict):
            continue
        seen_keys.append((codec, list(stream_data.keys()), list(data.keys())))
        for quality, rendition in data.items():
            main = rendition.get("main") if isinstance(rendition, dict) else None
            if not isinstance(main, dict):
                continue
            for fmt in ("flv", "hls"):
                url = main.get(fmt)
                if isinstance(url, str) and url:
                    out.append({
                        "codec": codec,
                        "quality": quality,
                        "fmt": fmt,
                        "url": url,
                    })
    return out, seen_keys


def parse_live_status(payload):
    if not isinstance(payload, dict):
        raise ValueError("TikTok status response has an invalid format")
    data = payload.get("data")
    user = data.get("user") if isinstance(data, dict) else None
    if not isinstance(user, dict) or not isinstance(user.get("status"), int):
        raise ValueError("TikTok status response is missing the user status")

    status = user["status"]
    if status == 2:
        return True
    if status != 3 or not isinstance(data, dict):
        return False

    room = data.get("liveRoom")
    if not isinstance(room, dict) or room.get("status") not in {2, 3}:
        return False
    candidates, _ = stream_candidates(room)
    return bool(user.get("roomId") and candidates)
