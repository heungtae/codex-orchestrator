import asyncio
import logging
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts.telegram_polling_runner import (
    _ACTIVE_NOTIFICATION_HANDLER,
    _CodexEventValidationFilter,
    _load_allowed_users_from_conf,
    _next_offset_from_updates,
    _run_polling,
    _resolve_conf_path,
    _render_progress_message,
    _run_with_progress_notifications,
)


class _FakeTelegramApi:
    def __init__(self) -> None:
        self.messages: list[tuple[str, str]] = []

    def send_message(self, *, chat_id: str, text: str) -> None:
        self.messages.append((chat_id, text))


class _SlowOrchestrator:
    def __init__(self, delay_sec: float, output: str) -> None:
        self.delay_sec = delay_sec
        self.output = output

    async def handle_message(self, chat_id: str, user_id: str, text: str) -> str:
        await asyncio.sleep(self.delay_sec)
        return self.output


class TelegramPollingRunnerProgressTests(unittest.TestCase):
    def test_run_polling_creates_conf_before_token_validation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            conf_path = Path(tmp) / "first-run-conf.toml"
            env = {"CODEX_CONF_PATH": str(conf_path)}
            with patch.dict("os.environ", env, clear=True):
                with self.assertRaises(SystemExit) as exc:
                    asyncio.run(_run_polling())
            self.assertEqual(str(exc.exception), "TELEGRAM_BOT_TOKEN is required")
            self.assertTrue(conf_path.exists())
            self.assertIn("[telegram]", conf_path.read_text(encoding="utf-8"))

    def test_load_allowed_users_from_conf_creates_file_when_missing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "missing-conf.toml"
            self.assertIsNone(_load_allowed_users_from_conf(str(path)))
            self.assertTrue(path.exists())
            self.assertIn("[telegram]", path.read_text(encoding="utf-8"))

    def test_resolve_conf_path_expands_tilde(self) -> None:
        home = Path.home()
        resolved = _resolve_conf_path("~/.codex-orchestrator/conf.toml")
        self.assertEqual(resolved, (home / ".codex-orchestrator" / "conf.toml").resolve())

    def test_load_allowed_users_from_conf_parses_values(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "conf.toml"
            path.write_text(
                """
[telegram]
allowed_users = [123456789, "987654321"]
""".strip(),
                encoding="utf-8",
            )
            parsed = _load_allowed_users_from_conf(str(path))
            self.assertEqual(parsed, {"123456789", "987654321"})

    def test_load_allowed_users_from_conf_rejects_invalid_type(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "conf.toml"
            path.write_text(
                """
[telegram]
allowed_users = "123456789"
""".strip(),
                encoding="utf-8",
            )
            with self.assertRaises(ValueError):
                _load_allowed_users_from_conf(str(path))

    def test_render_progress_message_uses_template(self) -> None:
        text = _render_progress_message(
            template="working {elapsed_sec}s #{progress_count}",
            elapsed_sec=12,
            progress_count=2,
        )
        self.assertEqual(text, "working 12s #2")

    def test_render_progress_message_falls_back_on_bad_template(self) -> None:
        text = _render_progress_message(
            template="{missing",
            elapsed_sec=9,
            progress_count=1,
        )
        self.assertEqual(text, "still working... elapsed=9s")

    def test_progress_notification_is_skipped_for_fast_request(self) -> None:
        orchestrator = _SlowOrchestrator(delay_sec=0.03, output="done")
        api = _FakeTelegramApi()

        output = asyncio.run(
            _run_with_progress_notifications(
                orchestrator=orchestrator,
                api=api,
                chat_id="100",
                user_id="200",
                text="hello",
                enabled=True,
                initial_delay_sec=0.2,
                interval_sec=0.1,
                message_template="working {elapsed_sec}s",
            )
        )

        self.assertEqual(output, "done")
        self.assertEqual(api.messages, [])

    def test_slow_request_does_not_emit_synthetic_progress_message(self) -> None:
        orchestrator = _SlowOrchestrator(delay_sec=0.28, output="done")
        api = _FakeTelegramApi()

        output = asyncio.run(
            _run_with_progress_notifications(
                orchestrator=orchestrator,
                api=api,
                chat_id="100",
                user_id="200",
                text="hello",
                enabled=True,
                initial_delay_sec=0.05,
                interval_sec=0.05,
                message_template="working {elapsed_sec}s #{progress_count}",
            )
        )

        self.assertEqual(output, "done")
        self.assertEqual(api.messages, [])

    def test_next_offset_from_updates_returns_latest_plus_one(self) -> None:
        updates = [
            {"update_id": 10},
            {"update_id": 14},
            {"update_id": 11},
        ]
        self.assertEqual(_next_offset_from_updates(updates), 15)

    def test_next_offset_from_updates_ignores_invalid_update_id(self) -> None:
        updates = [
            {"update_id": "10"},
            {"foo": "bar"},
        ]
        self.assertIsNone(_next_offset_from_updates(updates))

    def test_codex_event_validation_filter_prints_agent_text_notification(self) -> None:
        filter_ = _CodexEventValidationFilter()
        record = logging.LogRecord(
            name="root",
            level=logging.WARNING,
            pathname=__file__,
            lineno=1,
            msg=(
                "Failed to validate notification: validation error. "
                "Message was: method='codex/event' "
                "params={'msg': {'type': 'item_completed', 'item': {'type': 'AgentMessage', "
                "'id': 'msg_1', 'content': [{'type': 'Text', 'text': 'hello world'}], "
                "'phase': 'commentary'}}} jsonrpc='2.0'"
            ),
            args=(),
            exc_info=None,
        )

        with patch("builtins.print") as mocked_print:
            allowed = filter_.filter(record)

        self.assertFalse(allowed)
        self.assertEqual(mocked_print.call_count, 1)
        printed = mocked_print.call_args.args[0]
        self.assertIn("[codex-notification]", printed)
        self.assertIn("id=msg_1", printed)
        self.assertIn("phase=commentary", printed)
        self.assertIn("text=hello world", printed)
        self.assertEqual(mocked_print.call_args.kwargs, {"flush": True})

    def test_codex_event_validation_filter_prints_final_answer_to_stdout(self) -> None:
        filter_ = _CodexEventValidationFilter()
        record = logging.LogRecord(
            name="root",
            level=logging.WARNING,
            pathname=__file__,
            lineno=1,
            msg=(
                "Failed to validate notification: validation error. "
                "Message was: method='codex/event' "
                "params={'msg': {'type': 'item_completed', 'item': {'type': 'AgentMessage', "
                "'id': 'msg_final', 'content': [{'type': 'Text', 'text': 'done'}], "
                "'phase': 'final_answer'}}} jsonrpc='2.0'"
            ),
            args=(),
            exc_info=None,
        )

        with patch("builtins.print") as mocked_print:
            allowed = filter_.filter(record)

        self.assertFalse(allowed)
        self.assertEqual(mocked_print.call_count, 1)
        printed = mocked_print.call_args.args[0]
        self.assertIn("id=msg_final", printed)
        self.assertIn("phase=final_answer", printed)
        self.assertIn("text=done", printed)
        self.assertEqual(mocked_print.call_args.kwargs, {"flush": True})

    def test_codex_event_validation_filter_dispatches_non_final_only(self) -> None:
        filter_ = _CodexEventValidationFilter()
        delivered: list[tuple[str, str, str]] = []

        def _capture(notification: object) -> None:
            delivered.append(
                (
                    getattr(notification, "message_id"),
                    getattr(notification, "phase"),
                    getattr(notification, "text"),
                )
            )

        commentary = logging.LogRecord(
            name="root",
            level=logging.WARNING,
            pathname=__file__,
            lineno=1,
            msg=(
                "Failed to validate notification: validation error. "
                "Message was: method='codex/event' "
                "params={'msg': {'type': 'item_completed', 'item': {'type': 'AgentMessage', "
                "'id': 'msg_mid', 'content': [{'type': 'Text', 'text': 'progress'}], "
                "'phase': 'commentary'}}} jsonrpc='2.0'"
            ),
            args=(),
            exc_info=None,
        )
        final_answer = logging.LogRecord(
            name="root",
            level=logging.WARNING,
            pathname=__file__,
            lineno=1,
            msg=(
                "Failed to validate notification: validation error. "
                "Message was: method='codex/event' "
                "params={'msg': {'type': 'item_completed', 'item': {'type': 'AgentMessage', "
                "'id': 'msg_final', 'content': [{'type': 'Text', 'text': 'final'}], "
                "'phase': 'final_answer'}}} jsonrpc='2.0'"
            ),
            args=(),
            exc_info=None,
        )

        token = _ACTIVE_NOTIFICATION_HANDLER.set(_capture)
        try:
            with patch("builtins.print"):
                self.assertFalse(filter_.filter(commentary))
                self.assertFalse(filter_.filter(final_answer))
        finally:
            _ACTIVE_NOTIFICATION_HANDLER.reset(token)

        self.assertEqual(delivered, [("msg_mid", "commentary", "progress")])

    def test_codex_event_validation_filter_ignores_other_event_types(self) -> None:
        filter_ = _CodexEventValidationFilter()
        record = logging.LogRecord(
            name="root",
            level=logging.WARNING,
            pathname=__file__,
            lineno=1,
            msg=(
                "Failed to validate notification: validation error. "
                "Message was: method='codex/event' "
                "params={'msg': {'type': 'item_started', 'item': {'type': 'AgentMessage', "
                "'id': 'msg_1', 'content': [], 'phase': 'commentary'}}} jsonrpc='2.0'"
            ),
            args=(),
            exc_info=None,
        )

        with patch("builtins.print") as mocked_print:
            allowed = filter_.filter(record)

        self.assertFalse(allowed)
        mocked_print.assert_not_called()

    def test_codex_event_validation_filter_allows_other_logs(self) -> None:
        filter_ = _CodexEventValidationFilter()
        record = logging.LogRecord(
            name="root",
            level=logging.WARNING,
            pathname=__file__,
            lineno=1,
            msg="some other warning",
            args=(),
            exc_info=None,
        )

        with patch("builtins.print") as mocked_print:
            allowed = filter_.filter(record)

        self.assertTrue(allowed)
        mocked_print.assert_not_called()


if __name__ == "__main__":
    unittest.main()
