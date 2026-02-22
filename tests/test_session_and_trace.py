import asyncio
import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from core.session_manager import SessionManager
from core.trace_logger import TraceLogger


class SessionAndTraceTests(unittest.TestCase):
    def test_session_manager_file_persistence_and_reset(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            manager = SessionManager(base_dir=tmp_path / "sessions")

            session = asyncio.run(manager.load(chat_id="100", user_id="200"))
            self.assertEqual(session.mode, "single")

            session.mode = "multi"
            session.history = [{"role": "user", "content": "hello"}]
            asyncio.run(manager.save(session))

            loaded = asyncio.run(manager.load(chat_id="100", user_id="200"))
            self.assertEqual(loaded.mode, "multi")
            self.assertEqual(loaded.history, [{"role": "user", "content": "hello"}])

            reset = asyncio.run(manager.reset(chat_id="100", user_id="200"))
            self.assertEqual(reset.mode, "single")
            self.assertEqual(reset.history, [])

            session_file = tmp_path / "sessions" / "100-200.json"
            self.assertTrue(session_file.exists())

    def test_trace_logger_masks_sensitive_values(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            logger = TraceLogger(base_dir=tmp_path / "traces")

            logger.append(
                {
                    "run_id": "run-1",
                    "session_id": "tg:1:1",
                    "mode": "single",
                    "input_kind": "text",
                    "input_text": "authorization=abc token=xyz",
                    "output_text": "api_key=secret",
                    "status": "ok",
                    "latency_ms": 12,
                }
            )

            date_name = datetime.now(timezone.utc).date().isoformat()
            path = tmp_path / "traces" / f"{date_name}.jsonl"
            self.assertTrue(path.exists())

            line = path.read_text(encoding="utf-8").strip()
            payload = json.loads(line)
            self.assertIn("authorization=***", payload["input_text"])
            self.assertIn("token=***", payload["input_text"])
            self.assertEqual(payload["output_text"], "api_key=***")


if __name__ == "__main__":
    unittest.main()
