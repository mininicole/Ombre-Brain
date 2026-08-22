import sqlite3
import unittest
from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory

from handoff_store import HandoffStore


class HandoffStoreTests(unittest.TestCase):
    def setUp(self):
        self.folder = TemporaryDirectory()
        self.db_path = str(Path(self.folder.name) / "embeddings.db")
        self.store = HandoffStore(self.db_path, {"gale"})

    def tearDown(self):
        self.folder.cleanup()

    def test_partial_update_preserves_omitted_fields_and_refreshes_timestamp(self):
        created = self.store.update(
            "gale",
            current_topic="Handoff v1",
            active_goal="完成轻量交接模块",
            unresolved=["实现 store", "注册 tools"],
            status="pending",
            now=1_000,
        )["handoff"]
        updated = self.store.update(
            "gale",
            current_state="store 已完成",
            now=1_100,
        )["handoff"]

        self.assertEqual("Handoff v1", updated["current_topic"])
        self.assertEqual("完成轻量交接模块", updated["active_goal"])
        self.assertEqual(["实现 store", "注册 tools"], updated["unresolved"])
        self.assertEqual("pending", updated["status"])
        self.assertEqual("store 已完成", updated["current_state"])
        self.assertNotEqual(created["updated_at"], updated["updated_at"])

    def test_explicit_empty_values_clear_fields_and_expiry(self):
        self.store.update(
            "gale",
            current_topic="待清空",
            unresolved=["事项"],
            expires_at="2030-01-01T00:00:00+08:00",
            now=1_000,
        )
        handoff = self.store.update(
            "gale",
            current_topic="",
            unresolved=[],
            expires_at="",
            now=1_100,
        )["handoff"]

        self.assertEqual("", handoff["current_topic"])
        self.assertEqual([], handoff["unresolved"])
        self.assertIsNone(handoff["expires_at"])

    def test_read_returns_empty_for_expired_or_terminal_records(self):
        self.store.update(
            "gale",
            active_goal="短期目标",
            expires_at="1970-01-01T00:20:00Z",
            now=1_000,
        )
        expired = self.store.read("gale", now=1_300)
        self.assertFalse(expired["active"])
        self.assertEqual("expired", expired["reason"])

        self.store.update("gale", status="done", expires_at="", now=1_400)
        done = self.store.read("gale", now=1_500)
        self.assertFalse(done["active"])
        self.assertEqual("inactive", done["reason"])
        self.assertEqual("done", done["status"])

    def test_complete_item_then_complete_whole_handoff(self):
        self.store.update(
            "gale",
            unresolved=["实现 store", "注册 tools"],
            now=1_000,
        )
        item_result = self.store.complete("gale", "实现 store", now=1_100)
        self.assertTrue(item_result["completed"])
        self.assertEqual(["注册 tools"], item_result["handoff"]["unresolved"])
        self.assertEqual("active", item_result["handoff"]["status"])

        whole_result = self.store.complete("gale", now=1_200)
        self.assertTrue(whole_result["completed"])
        self.assertEqual("done", whole_result["handoff"]["status"])
        self.assertFalse(self.store.read("gale", now=1_300)["active"])

    def test_complete_missing_item_does_not_mutate_handoff(self):
        original = self.store.update(
            "gale", unresolved=["保留事项"], now=1_000
        )["handoff"]
        result = self.store.complete("gale", "不存在", now=1_100)
        current = self.store.read("gale", now=1_200)["handoff"]

        self.assertFalse(result["completed"])
        self.assertEqual("item_not_found", result["reason"])
        self.assertEqual(original["updated_at"], current["updated_at"])
        self.assertEqual(["保留事项"], current["unresolved"])

    def test_expire_stale_changes_only_old_active_or_pending_records(self):
        original = self.store.update("gale", status="pending", now=1_000)["handoff"]
        recent = self.store.expire_stale("gale", 600, now=1_500)
        self.assertFalse(recent["staled"])
        self.assertEqual("recent", recent["reason"])

        stale = self.store.expire_stale("gale", 600, now=1_600)
        self.assertTrue(stale["staled"])
        self.assertEqual("stale", stale["status"])
        inactive = self.store.read("gale", now=1_700)
        self.assertEqual("stale", inactive["status"])

        with closing(sqlite3.connect(self.db_path)) as connection, connection:
            stored_updated_at = connection.execute(
                "SELECT updated_at FROM handoffs WHERE agent_id = 'gale'"
            ).fetchone()[0]
        self.assertEqual(original["updated_at"], stored_updated_at)

        self.store.update("gale", status="blocked", now=2_000)
        blocked = self.store.expire_stale("gale", 600, now=5_000)
        self.assertFalse(blocked["staled"])
        self.assertEqual("status_not_eligible", blocked["reason"])

    def test_clear_deletes_only_the_selected_handoff(self):
        self.store.update("gale", active_goal="会被清空", now=1_000)
        self.assertTrue(self.store.clear("gale")["cleared"])
        self.assertFalse(self.store.clear("gale")["cleared"])
        self.assertEqual("missing", self.store.read("gale", now=1_100)["reason"])

    def test_agent_allow_list_is_data_driven(self):
        with self.assertRaisesRegex(ValueError, "allowed: gale"):
            self.store.read("evan")

        evan_store = HandoffStore(self.db_path, {"evan"})
        evan_store.update("evan", active_goal="未来扩展", now=1_000)
        self.assertTrue(evan_store.read("evan", now=1_100)["active"])

    def test_handoffs_table_does_not_touch_existing_embeddings_table(self):
        isolated_db = str(Path(self.folder.name) / "shared.db")
        with closing(sqlite3.connect(isolated_db)) as connection, connection:
            connection.execute(
                """
                CREATE TABLE embeddings (
                    bucket_id TEXT PRIMARY KEY,
                    embedding TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            connection.execute(
                "INSERT INTO embeddings VALUES ('bucket-1', '[0.1]', 'before')"
            )

        store = HandoffStore(isolated_db, {"gale"})
        store.update("gale", current_topic="独立表", now=1_000)

        with closing(sqlite3.connect(isolated_db)) as connection, connection:
            embedding = connection.execute(
                "SELECT bucket_id, embedding, updated_at FROM embeddings"
            ).fetchone()
            tables = {
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                )
            }
        self.assertEqual(("bucket-1", "[0.1]", "before"), embedding)
        self.assertIn("handoffs", tables)

    def test_size_and_status_validation_keep_handoff_small(self):
        with self.assertRaisesRegex(ValueError, "current_topic"):
            self.store.update("gale", current_topic="x" * 161)
        with self.assertRaisesRegex(ValueError, "at most 20"):
            self.store.update("gale", unresolved=[str(i) for i in range(21)])
        with self.assertRaisesRegex(ValueError, "handoff content"):
            self.store.update(
                "gale",
                unresolved=[f"u{i}-" + ("x" * 295) for i in range(20)],
                recent_decisions=[f"d{i}-" + ("x" * 295) for i in range(20)],
            )
        with self.assertRaisesRegex(ValueError, "status"):
            self.store.update("gale", status="unknown")


if __name__ == "__main__":
    unittest.main()
