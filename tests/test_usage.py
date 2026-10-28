import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "linux"))

from ds_pet.usage import UsageReader, format_count


def line(value):
    return json.dumps(value, ensure_ascii=False) + "\n"


class UsageTests(unittest.TestCase):
    def test_primary_session_and_incremental_totals(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            primary = root / "2026" / "01" / "rollout-main.jsonl"
            primary.parent.mkdir(parents=True)
            primary.write_text(
                line({"type": "session_meta", "payload": {"id": "main", "source": "cli"}})
                + line(
                    {
                        "type": "event_msg",
                        "timestamp": "2026-01-01T00:00:00Z",
                        "payload": {
                            "type": "token_count",
                            "info": {
                                "total_token_usage": {
                                    "input_tokens": 100,
                                    "cached_input_tokens": 40,
                                    "output_tokens": 20,
                                    "reasoning_output_tokens": 5,
                                },
                                "last_token_usage": {
                                    "input_tokens": 100,
                                    "cached_input_tokens": 40,
                                    "output_tokens": 20,
                                    "reasoning_output_tokens": 5,
                                },
                            },
                            "request_id": "one",
                        },
                    }
                ),
                encoding="utf-8",
            )
            subagent = root / "rollout-sub.jsonl"
            subagent.write_text(
                line(
                    {
                        "type": "session_meta",
                        "payload": {"id": "sub", "parent_thread_id": "main", "source": {"subagent": {}}},
                    }
                ),
                encoding="utf-8",
            )
            reader = UsageReader(root)
            self.assertEqual(reader.newest_primary(), primary)
            reader.update()
            self.assertEqual((reader.input, reader.cached, reader.output, reader.reasoning), (100, 40, 20, 5))
            self.assertEqual(reader.requests, 1)
            with primary.open("a", encoding="utf-8") as handle:
                handle.write(
                    line(
                        {
                            "type": "event_msg",
                            "timestamp": "2026-01-01T00:01:00Z",
                            "payload": {
                                "type": "token_usage_record",
                                "usage": {
                                    "input_tokens": 50,
                                    "cached_input_tokens": 10,
                                    "output_tokens": 30,
                                    "reasoning_output_tokens": 0,
                                },
                                "request_id": "two",
                            },
                        }
                    )
                )
            reader.update()
            self.assertEqual((reader.input, reader.cached, reader.output, reader.reasoning), (150, 50, 50, 5))
            self.assertEqual(reader.requests, 2)
            reader.update()
            self.assertEqual(reader.requests, 2)
            self.assertAlmostEqual(reader.cache_ratio(), 50 / 150)

    def test_format_count(self):
        self.assertEqual(format_count(999), "999")
        self.assertEqual(format_count(1234), "1k")
        self.assertEqual(format_count(58800000), "58.8M")


if __name__ == "__main__":
    unittest.main()
