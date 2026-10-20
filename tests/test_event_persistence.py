import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from events import BiteEvent, EventStore, timestamp


class PersistenceTests(unittest.TestCase):
    def test_restart_preserves_totals_beyond_ui_and_old_file_limit(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "events.jsonl"
            store = EventStore(path)
            for i in range(600):
                bite = BiteEvent(timestamp(), "上钩！", .95, (0, 0, 10, 10), 0)
                store.bite(bite)
                bite.result = "SUCCESS" if i % 2 == 0 else "FAILURE"
                bite.fish = f"鱼-{i}" if i % 2 == 0 else ""
                store.bite(bite)
            # Push every bite outside the former 2 MB startup read window.
            for i in range(550):
                store.add("log", {"text": "x" * 4000, "id": str(i)})
            expected = store.stats()
            store.close()
            store.close()
            self.assertEqual(expected["bites"], 600)
            self.assertEqual(expected["success"], 300)
            self.assertEqual(expected["last_fish"], "鱼-598")
            snapshot = json.loads(store.snapshot_path.read_text(encoding="utf-8"))
            self.assertEqual(snapshot["stats"], expected)
            self.assertEqual(len(snapshot["history"]), 500)
            restored = EventStore(path)
            try:
                self.assertEqual(restored.stats(), expected)
                self.assertEqual(len(restored.history()), 500)
                bite = BiteEvent(timestamp(), "上钩！", .9, (), 0, result="UNKNOWN")
                restored.bite(bite)
            finally:
                restored.close()
            again = EventStore(path)
            try:
                self.assertEqual(again.stats()["bites"], 601)
                self.assertEqual(again.stats()["unknown"], 1)
            finally:
                again.close()

    def test_bad_records_do_not_hide_valid_history(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "events.jsonl"
            path.write_bytes(b'broken\nnull\n[]\n' + json.dumps({
                "type": "bite", "id": "one", "result": "SUCCESS", "fish": "鲤鱼"
            }, ensure_ascii=False).encode("utf-8") + b'\n{"incomplete":')
            store = EventStore(path)
            try:
                self.assertEqual(store.stats()["success"], 1)
                self.assertEqual(store.stats()["last_fish"], "鲤鱼")
            finally:
                store.close()

    def test_snapshot_failure_keeps_previous_snapshot_and_event_history(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "events.jsonl"
            store = EventStore(path)
            store.snapshot_path.write_text("previous", encoding="utf-8")
            store.bite(BiteEvent(timestamp(), "上钩！", .9, (), 0))
            with patch("events.os.replace", side_effect=OSError("disk error")):
                with self.assertRaises(OSError):
                    store.close()
            self.assertEqual(store.snapshot_path.read_text(encoding="utf-8"), "previous")
            restored = EventStore(path)
            try:
                self.assertEqual(restored.stats()["bites"], 1)
            finally:
                restored.close()


if __name__ == "__main__":
    unittest.main()
