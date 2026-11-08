import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "engine"))
import resolve


class DescribeResponseTests(unittest.TestCase):
    def test_summary_never_contains_urls_ids_or_text(self):
        d = {"data": {
            "user": {"status": 2, "roomId": 7705831818389441312, "uniqueId": "someone"},
            "liveRoom": {
                "status": 2, "isAgeRestricted": True, "title": "private title",
                "streamData": {"pull_data": {"stream_data": "https://example.com/live.flv?token=abc"}},
            },
        }}
        text = json.dumps(resolve.describe_response(d))
        for secret in ("https://", "7705831818389441312", "private title", "token=abc", "someone"):
            self.assertNotIn(secret, text)
        self.assertIn("isAgeRestricted", text)

    def test_live_room_with_no_stream_data(self):
        r = resolve.describe_response({"data": {"user": {"status": 2}, "liveRoom": {}}})
        self.assertFalse(r["room_present"])
        self.assertEqual(r["room_keys"], [])

    def test_unexpected_shapes_do_not_raise(self):
        self.assertEqual(resolve.describe_response(None), {"type": "NoneType"})
        resolve.describe_response({"data": "oops"})


if __name__ == "__main__":
    unittest.main()
