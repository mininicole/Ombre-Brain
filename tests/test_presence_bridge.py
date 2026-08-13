import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from presence_bridge import read_presence, write_presence


class PresenceBridgeTests(unittest.TestCase):
    def test_presence_round_trip_is_short_lived_and_separate(self):
        with TemporaryDirectory() as folder:
            path = Path(folder) / ".guardian_presence.json"
            saved = write_presence(
                str(path),
                "  official   Guardian context  ",
                now=1_000,
                ttl_seconds=600,
            )
            self.assertEqual(saved["topic"], "official Guardian context")
            self.assertTrue(read_presence(str(path), now=1_500)["active"])
            self.assertEqual(
                read_presence(str(path), now=1_601),
                {"active": False, "reason": "expired"},
            )

    def test_presence_rejects_empty_topic(self):
        with TemporaryDirectory() as folder:
            path = Path(folder) / ".guardian_presence.json"
            with self.assertRaisesRegex(ValueError, "topic"):
                write_presence(str(path), "  ")

    def test_presence_fails_closed_on_invalid_or_future_data(self):
        with TemporaryDirectory() as folder:
            path = Path(folder) / ".guardian_presence.json"
            path.write_text("not json", encoding="utf-8")
            self.assertFalse(read_presence(str(path), now=1_000)["active"])

            path.write_text(
                json.dumps(
                    {
                        "topic": "context",
                        "last_user_at": "1970-01-01T00:30:00Z",
                        "expires_at": "1970-01-01T01:00:00Z",
                    }
                ),
                encoding="utf-8",
            )
            self.assertEqual(
                read_presence(str(path), now=1_000),
                {"active": False, "reason": "future_timestamp"},
            )


if __name__ == "__main__":
    unittest.main()
