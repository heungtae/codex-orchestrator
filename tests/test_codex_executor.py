import asyncio
import unittest

from integrations.codex_executor import CodexExecutionError, CodexMcpExecutor


class _Tracker:
    def __init__(self) -> None:
        self.running = True
        self.ready = True
        self.stopped_calls = 0
        self.errors: list[str] = []

    def mark_running(self, pid=None, ready: bool = False) -> None:
        self.running = True
        self.ready = ready

    def mark_stopped(self) -> None:
        self.stopped_calls += 1
        self.running = False
        self.ready = False

    def record_error(self, message: str) -> None:
        self.errors.append(message)


class _FailingSession:
    async def call_tool(self, tool_name, payload):
        raise RuntimeError("broken pipe")


class _NoopAsyncContext:
    async def __aexit__(self, exc_type, exc, tb) -> bool:
        return False


class CodexMcpExecutorTests(unittest.TestCase):
    def test_transport_error_marks_status_stopped(self) -> None:
        tracker = _Tracker()
        executor = CodexMcpExecutor(status_tracker=tracker)
        executor._started = True
        executor._session = _FailingSession()
        executor._session_cm = _NoopAsyncContext()
        executor._stdio_cm = _NoopAsyncContext()

        with self.assertRaises(CodexExecutionError):
            asyncio.run(executor.run(prompt="hello"))

        self.assertFalse(executor._started)
        self.assertIsNone(executor._session)
        self.assertFalse(tracker.running)
        self.assertFalse(tracker.ready)
        self.assertGreaterEqual(tracker.stopped_calls, 1)
        self.assertTrue(any("broken pipe" in message for message in tracker.errors))


if __name__ == "__main__":
    unittest.main()
