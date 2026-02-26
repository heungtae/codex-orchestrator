import asyncio
import io
import unittest
from contextlib import redirect_stdout

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


class _CancelledSession:
    async def call_tool(self, tool_name, payload):
        raise asyncio.CancelledError()


class _TextResponseSession:
    async def call_tool(self, tool_name, payload):
        return {"content": [{"type": "text", "text": "first"}, {"type": "text", "text": "second"}]}


class _StructuredResponseSession:
    async def call_tool(self, tool_name, payload):
        return {"structuredContent": {"content": "structured message"}}


class _NoopAsyncContext:
    async def __aexit__(self, exc_type, exc, tb) -> bool:
        return False


class _TrackingAsyncContext:
    def __init__(self) -> None:
        self.exit_calls = 0

    async def __aexit__(self, exc_type, exc, tb) -> bool:
        self.exit_calls += 1
        return False


class _FailingAsyncContext:
    async def __aexit__(self, exc_type, exc, tb) -> bool:
        raise RuntimeError("close failed")


class _SlowAsyncContext:
    def __init__(self, delay_sec: float) -> None:
        self.delay_sec = delay_sec

    async def __aexit__(self, exc_type, exc, tb) -> bool:
        await asyncio.sleep(self.delay_sec)
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

    def test_cancelled_call_resets_executor_state(self) -> None:
        tracker = _Tracker()
        executor = CodexMcpExecutor(status_tracker=tracker)
        session_cm = _TrackingAsyncContext()
        stdio_cm = _TrackingAsyncContext()
        executor._started = True
        executor._session = _CancelledSession()
        executor._session_cm = session_cm
        executor._stdio_cm = stdio_cm

        with self.assertRaises(asyncio.CancelledError):
            asyncio.run(executor.run(prompt="hello"))

        self.assertFalse(executor._started)
        self.assertIsNone(executor._session)
        self.assertIsNone(executor._session_cm)
        self.assertIsNone(executor._stdio_cm)
        self.assertEqual(session_cm.exit_calls, 1)
        self.assertEqual(stdio_cm.exit_calls, 1)
        self.assertFalse(tracker.running)
        self.assertFalse(tracker.ready)
        self.assertGreaterEqual(tracker.stopped_calls, 1)
        self.assertTrue(any("request cancelled" in message for message in tracker.errors))

    def test_close_resets_state_even_when_session_close_fails(self) -> None:
        executor = CodexMcpExecutor()
        executor._started = True
        executor._session = object()
        executor._session_cm = _FailingAsyncContext()
        executor._stdio_cm = _NoopAsyncContext()

        with self.assertRaises(RuntimeError):
            asyncio.run(executor.close())

        self.assertFalse(executor._started)
        self.assertIsNone(executor._session)
        self.assertIsNone(executor._session_cm)
        self.assertIsNone(executor._stdio_cm)

    def test_close_times_out_and_resets_state(self) -> None:
        executor = CodexMcpExecutor(close_timeout_seconds=0.01)
        executor._started = True
        executor._session = object()
        executor._session_cm = _SlowAsyncContext(delay_sec=0.5)
        executor._stdio_cm = _NoopAsyncContext()

        with self.assertRaises(RuntimeError):
            asyncio.run(executor.close())

        self.assertFalse(executor._started)
        self.assertIsNone(executor._session)
        self.assertIsNone(executor._session_cm)
        self.assertIsNone(executor._stdio_cm)

    def test_run_prints_all_text_response_messages(self) -> None:
        executor = CodexMcpExecutor()
        executor._started = True
        executor._session = _TextResponseSession()

        captured = io.StringIO()
        with redirect_stdout(captured):
            output = asyncio.run(executor.run(prompt="hello"))

        self.assertEqual(output, "first\nsecond")
        logs = captured.getvalue()
        self.assertIn("[codex mcp-response] first", logs)
        self.assertIn("[codex mcp-response] second", logs)

    def test_run_prints_structured_response_message(self) -> None:
        executor = CodexMcpExecutor()
        executor._started = True
        executor._session = _StructuredResponseSession()

        captured = io.StringIO()
        with redirect_stdout(captured):
            output = asyncio.run(executor.run(prompt="hello"))

        self.assertEqual(output, "structured message")
        self.assertIn("[codex mcp-response] structured message", captured.getvalue())


if __name__ == "__main__":
    unittest.main()
