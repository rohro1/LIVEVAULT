import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "engine"))

from record import capture_has_data, ffmpeg_error_summary


class RecorderTests(unittest.TestCase):
    def test_capture_requires_nonempty_segment_data(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            directory = Path(temp_dir)
            segment = directory / "part_001.ts"
            segment.touch()
            self.assertFalse(capture_has_data(directory))

            segment.write_bytes(b"stream data")
            self.assertTrue(capture_has_data(directory))

    def test_ffmpeg_error_summary_redacts_source_urls(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            log = Path(temp_dir) / "ffmpeg.log"
            log.write_text("Unable to open https://example.test/live?token=secret\n")

            summary = ffmpeg_error_summary(log)

        self.assertIn("[URL]", summary)
        self.assertNotIn("secret", summary)


if __name__ == "__main__":
    unittest.main()
