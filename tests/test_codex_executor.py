import asyncio
import io
import unittest
from contextlib import redirect_stdout

from integrations.codex_executor import (
    AgentTextNotification,
    CodexExecutionError,
    OpenAIAgentsExecutor,
)


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


class _FakeServer:
    def __init__(self, *, tools: list[str], result=None, error: Exception | None = None) -> None:
        self._tools = tools
        self._result = result
        self._error = error
        self.entered = False
        self.exited = False
        self.called_payload = None

    async def __aenter__(self):
        self.entered = True
        return self

    async def __aexit__(self, exc_type, exc, tb):
        self.exited = True
        return False

    async def list_tools(self):
        return [type("Tool", (), {"name": name}) for name in self._tools]

    async def call_tool(self, tool_name, payload):
        self.called_payload = payload
        if self._error is not None:
            raise self._error
        return self._result


class _SlowExitServer(_FakeServer):
    async def __aexit__(self, exc_type, exc, tb):
        await asyncio.sleep(0.5)
        self.exited = True
        return False


class _TestExecutor(OpenAIAgentsExecutor):
    def __init__(self, server: _FakeServer, **kwargs):
        super().__init__(**kwargs)
        self._injected_server = server

    def _create_mcp_server(self):
        return self._injected_server


class OpenAIAgentsExecutorTests(unittest.TestCase):
    def test_transport_error_marks_status_stopped(self) -> None:
        tracker = _Tracker()
        server = _FakeServer(tools=["codex"], error=RuntimeError("broken pipe"))
        executor = _TestExecutor(server=server, status_tracker=tracker)

        with self.assertRaises(CodexExecutionError):
            asyncio.run(executor.run(prompt="hello"))

        self.assertFalse(executor._started)
        self.assertIsNone(executor._server)
        self.assertFalse(tracker.running)
        self.assertFalse(tracker.ready)
        self.assertGreaterEqual(tracker.stopped_calls, 1)
        self.assertTrue(any("broken pipe" in message for message in tracker.errors))

    def test_cancelled_call_resets_executor_state(self) -> None:
        tracker = _Tracker()
        server = _FakeServer(tools=["codex"], error=asyncio.CancelledError())
        executor = _TestExecutor(server=server, status_tracker=tracker)

        with self.assertRaises(asyncio.CancelledError):
            asyncio.run(executor.run(prompt="hello"))

        self.assertFalse(executor._started)
        self.assertIsNone(executor._server)
        self.assertIsNone(executor._server_cm)
        self.assertFalse(tracker.running)
        self.assertFalse(tracker.ready)
        self.assertGreaterEqual(tracker.stopped_calls, 1)
        self.assertTrue(any("request cancelled" in message for message in tracker.errors))

    def test_close_times_out_and_resets_state(self) -> None:
        server = _SlowExitServer(tools=["codex"], result={"content": [{"text": "ok"}]})
        executor = _TestExecutor(server=server, close_timeout_seconds=0.01)
        asyncio.run(executor.warmup())

        with self.assertRaises(RuntimeError):
            asyncio.run(executor.close())

        self.assertFalse(executor._started)
        self.assertIsNone(executor._server)
        self.assertIsNone(executor._server_cm)

    def test_run_prints_all_text_response_messages(self) -> None:
        server = _FakeServer(
            tools=["codex"],
            result={"content": [{"type": "text", "text": "first"}, {"type": "text", "text": "second"}]},
        )
        executor = _TestExecutor(server=server)

        captured = io.StringIO()
        with redirect_stdout(captured):
            output = asyncio.run(executor.run(prompt="hello"))

        self.assertEqual(output, "first\nsecond")
        logs = captured.getvalue()
        self.assertIn("[codex mcp-response] first", logs)
        self.assertIn("[codex mcp-response] second", logs)

    def test_run_includes_policy_payload(self) -> None:
        server = _FakeServer(
            tools=["codex"],
            result={"content": [{"type": "text", "text": "done"}]},
        )
        executor = _TestExecutor(
            server=server,
            approval_policy="never",
            sandbox="workspace-write",
            default_model="gpt-5",
            cwd="/tmp/work",
        )

        output = asyncio.run(executor.run(prompt="hello", system_instructions="sys"))

        self.assertEqual(output, "done")
        self.assertEqual(server.called_payload["approval-policy"], "never")
        self.assertEqual(server.called_payload["sandbox"], "workspace-write")
        self.assertEqual(server.called_payload["model"], "gpt-5")
        self.assertEqual(server.called_payload["cwd"], "/tmp/work")
        self.assertEqual(server.called_payload["developer-instructions"], "sys")

    def test_warmup_fails_when_codex_tool_missing(self) -> None:
        server = _FakeServer(tools=["other"], result={"content": [{"text": "x"}]})
        executor = _TestExecutor(server=server)

        with self.assertRaises(CodexExecutionError):
            asyncio.run(executor.warmup())

    def test_extract_notification_from_event_params(self) -> None:
        params = {
            "msg": {
                "type": "item_completed",
                "item": {
                    "type": "AgentMessage",
                    "id": "msg_1",
                    "phase": "commentary",
                    "content": [{"type": "Text", "text": "step 1"}],
                    "agent_name": "plan.developer",
                },
            }
        }

        notification = OpenAIAgentsExecutor._extract_notification_from_event_params(params)
        self.assertEqual(
            notification,
            AgentTextNotification(
                message_id="msg_1",
                phase="commentary",
                text="step 1",
                agent_name="plan.developer",
            ),
        )

    def test_extract_notification_from_session_message(self) -> None:
        payload = {
            "method": "codex/event",
            "params": {
                "msg": {
                    "type": "item_completed",
                    "item": {
                        "type": "AgentMessage",
                        "id": "msg_2",
                        "phase": "final_answer",
                        "content": [{"type": "Text", "text": "done"}],
                    },
                }
            },
        }

        notification = OpenAIAgentsExecutor._extract_notification_from_session_message(payload)
        self.assertIsNotNone(notification)
        self.assertEqual(notification.message_id, "msg_2")
        self.assertEqual(notification.phase, "final_answer")
        self.assertEqual(notification.text, "done")


if __name__ == "__main__":
    unittest.main()
