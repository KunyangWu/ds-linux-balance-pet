import json
import sqlite3
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "linux"))

from ds_pet.tasks import TaskActivity, read_task_store


def create_store(home, started_at):
    state = sqlite3.connect(home / "state_5.sqlite")
    state.execute(
        "CREATE TABLE threads (id TEXT PRIMARY KEY, rollout_path TEXT, source TEXT, thread_source TEXT, archived INTEGER)"
    )
    state.execute(
        "INSERT INTO threads VALUES (?, ?, ?, ?, ?)",
        ("thread-1", str(home / "sessions" / "rollout-1.jsonl"), "cli", "cli", 0),
    )
    state.commit()
    state.close()
    history = sqlite3.connect(home / "thread_history_1.sqlite")
    history.execute(
        """
        CREATE TABLE thread_turns (
          thread_id TEXT, turn_id TEXT, root_turn_id TEXT, rollout_ordinal INTEGER, status TEXT,
          error_json TEXT, started_at INTEGER, completed_at INTEGER
        )
        """
    )
    history.execute(
        "INSERT INTO thread_turns VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        ("thread-1", "turn-1", "turn-1", 1, "inProgress", None, started_at, None),
    )
    history.commit()
    history.close()


class TaskTests(unittest.TestCase):
    def test_read_store_and_busy_state(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            sessions = home / "sessions"
            sessions.mkdir()
            started = int(datetime.now(timezone.utc).timestamp())
            create_store(home, started)
            rollout = sessions / "rollout-1.jsonl"
            rollout.write_text(
                json.dumps({"type": "session_meta", "payload": {"id": "thread-1", "source": "cli"}}) + "\n"
                + json.dumps(
                    {
                        "type": "event_msg",
                        "payload": {"type": "task_started", "turn_id": "turn-1", "started_at": "2026-01-01T00:00:00Z"},
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            store = read_task_store(home)
            self.assertTrue(store["available"])
            self.assertEqual(store["threads"][0]["thread_id"], "thread-1")
            activity = TaskActivity(sessions, home)
            old = datetime.fromtimestamp(started - 60, timezone.utc)
            with patch("ds_pet.tasks._runtime", return_value=(True, old)):
                result = activity.update()
            self.assertTrue(result["is_busy"])
            self.assertEqual(result["active_count"], 1)


if __name__ == "__main__":
    unittest.main()
