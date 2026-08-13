import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from presence_bridge import beat_presence_request, read_presence, write_presence


class PresenceBridgeTests(unittest.TestCase):
    def test_sources_do_not_overwrite_each_other(self):
        with TemporaryDirectory() as folder:
            path = str(Path(folder) / ".guardian_presence.json")
            write_presence(path, "聊一部电影", "chat", now=1_000)
            write_presence(path, "做一张海报", "work", now=1_100)
            write_presence(path, "this is discarded", "codex", now=1_200)
            result = read_presence(path, now=1_300)
            self.assertEqual("codex", result["latest_activity"]["source"])
            self.assertEqual("", result["latest_activity"]["topic"])
            self.assertEqual("work", result["latest_context"]["source"])
            self.assertEqual("做一张海报", result["latest_context"]["topic"])

    def test_chat_and_work_require_topic_but_codex_does_not(self):
        with TemporaryDirectory() as folder:
            path = str(Path(folder) / ".guardian_presence.json")
            with self.assertRaisesRegex(ValueError, "topic"):
                write_presence(path, "", "chat")
            self.assertEqual("", write_presence(path, "", "codex", now=1_000)["topic"])

    def test_expired_context_does_not_remove_newer_activity(self):
        with TemporaryDirectory() as folder:
            path = str(Path(folder) / ".guardian_presence.json")
            write_presence(path, "旧话题", "chat", now=1_000, ttl_seconds=60)
            write_presence(path, "", "codex", now=1_100, ttl_seconds=600)
            result = read_presence(path, now=1_200)
            self.assertEqual("codex", result["latest_activity"]["source"])
            self.assertIsNone(result["latest_context"])

    def test_legacy_codex_topic_is_never_exposed_as_context(self):
        with TemporaryDirectory() as folder:
            path = Path(folder) / ".guardian_presence.json"
            path.write_text(
                '{"version":1,"source":"codex","last_user_at":"1970-01-01T00:16:40Z",'
                '"expires_at":"1970-01-01T01:00:00Z","topic":"部署 Guardian"}',
                encoding="utf-8",
            )
            result = read_presence(str(path), now=1_100)
            self.assertEqual("", result["latest_activity"]["topic"])
            self.assertIsNone(result["latest_context"])

    def test_cached_beat_sources_map_to_presence_without_pulse_fields(self):
        self.assertEqual(
            {"source": "work", "topic": "正在验证 Work 上下文桥"},
            beat_presence_request(" 正在验证   Work 上下文桥 ", "workctx"),
        )
        self.assertEqual(
            {"source": "chat", "topic": "聊黑猫"},
            beat_presence_request("聊黑猫", "chatctx"),
        )
        self.assertIsNone(beat_presence_request("普通心跳", "cc"))
        with self.assertRaisesRegex(ValueError, "msg"):
            beat_presence_request("", "workctx")


if __name__ == "__main__":
    unittest.main()
