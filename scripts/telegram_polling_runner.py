#!/usr/bin/env python3
from __future__ import annotations

import ast
import asyncio
import builtins
import contextvars
import functools
import json
import logging
import os
import sys
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from bot.telegram_adapter import parse_update, split_telegram_text
from integrations.codex_executor import (
    CodexMcpExecutor,
    EchoCodexExecutor,
)
from main import build_orchestrator

_BLOCKING_POOL = ThreadPoolExecutor(max_workers=8)
_DEFAULT_CONF_PATH = Path.home() / ".codex-orchestrator" / "conf.toml"
_DEFAULT_CONF_TEMPLATE = (
    "[telegram]\n"
    "# Telegram from_user.id allowlist (int or string).\n"
    "# Set this to enable user-based access control.\n"
    "# allowed_users = [123456789]\n"
    "\n"
    "[telegram.polling]\n"
    "poll_timeout = 30\n"
    "loop_sleep_sec = 1\n"
    "delete_webhook_on_start = true\n"
    "drop_pending_updates = false\n"
    "ignore_pending_updates_on_start = true\n"
    "require_mcp_warmup = true\n"
    "cancel_wait_timeout_sec = 5\n"
    "\n"
    "[codex]\n"
    "mcp_command = \"npx\"\n"
    "mcp_args = \"-y codex mcp-server\"\n"
    "mcp_client_timeout_seconds = 360000\n"
    "# agent_model = \"gpt-5\"\n"
    "# agent_working_directory = \"~/develop/your-project\"\n"
    "allow_echo_executor = false\n"
    "approval_policy = \"never\"\n"
    "sandbox = \"danger-full-access\"\n"
    "mcp_direct_status = true\n"
    "# mcp_status_cmd = \"bash -lc \\\"echo running=true,ready=true,pid=12345,uptime_sec=30\\\"\"\n"
    "mcp_auto_detect_process = false\n"
    "\n"
    "[profile]\n"
    "default = \"default\"\n"
    "\n"
    "[profiles.default]\n"
    "model = \"gpt-5\"\n"
    "working_directory = \"~/develop/your-project\"\n"
    "\n"
    "# Optional: global agent overrides.\n"
    "# [agents.single.developer]\n"
    "# model = \"gpt-5-codex\"\n"
    "# system_prompt_file = \"./prompts/developer.txt\"\n"
    "\n"
    "# Optional: profile-specific overrides.\n"
    "# [profiles.default.agents.single.reviewer]\n"
    "# model = \"gpt-5\"\n"
    "# system_prompt = \"You are Reviewer Agent. Focus on concrete diffs and risks.\"\n"
    "\n"
    "[profiles.bridge]\n"
    "model = \"gpt-5\"\n"
    "working_directory = \"~/develop/bridge-project\"\n"
)
_UNAUTHORIZED_MESSAGE = "Unauthorized"


@dataclass(frozen=True)
class _AgentTextNotification:
    message_id: str
    phase: str
    text: str


@dataclass(frozen=True)
class _PollingConfig:
    poll_timeout: int = 30
    loop_sleep_sec: float = 1.0
    delete_webhook_on_start: bool = True
    drop_pending_updates: bool = False
    ignore_pending_updates_on_start: bool = True
    require_mcp_warmup: bool = True
    cancel_wait_timeout_sec: float = 5.0


@dataclass(frozen=True)
class _RunnerConfig:
    allowed_users: set[str] | None
    polling: _PollingConfig


_ACTIVE_NOTIFICATION_HANDLER: contextvars.ContextVar[
    Any | None
] = contextvars.ContextVar("codex_notification_handler", default=None)
_ACTIVE_NOTIFICATION_HANDLER_FALLBACK: Any | None = None


def _stdout_print(
    *values: object,
    **kwargs: Any,
) -> None:
    file = kwargs.get("file")
    target = sys.stdout if file is None else file
    if target is sys.stdout:
        timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
        if values:
            first, *rest = values
            values = (f"[{timestamp}] {first}", *rest)
        else:
            values = (f"[{timestamp}]",)

    builtins.print(*values, **kwargs)


@dataclass
class TelegramBotApi:
    token: str

    def __post_init__(self) -> None:
        self.base_url = f"https://api.telegram.org/bot{self.token}"

    def _post(self, method: str, payload: dict[str, Any]) -> Any:
        body = json.dumps(payload).encode("utf-8")
        request = urllib.request.Request(
            url=f"{self.base_url}/{method}",
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )

        try:
            with urllib.request.urlopen(request, timeout=70) as response:
                raw = response.read().decode("utf-8")
        except urllib.error.URLError as exc:
            raise RuntimeError(f"telegram request failed: {exc}") from exc

        try:
            payload_json = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"telegram response is not valid json: {raw}") from exc

        if not payload_json.get("ok"):
            description = payload_json.get("description", "unknown error")
            raise RuntimeError(f"telegram api error: {description}")

        return payload_json.get("result")

    def delete_webhook(self, drop_pending_updates: bool = False) -> None:
        self._post(
            "deleteWebhook",
            {
                "drop_pending_updates": drop_pending_updates,
            },
        )

    def get_updates(self, *, offset: int | None, timeout: int) -> list[dict[str, Any]]:
        payload: dict[str, Any] = {
            "timeout": timeout,
            "allowed_updates": ["message", "edited_message"],
        }
        if offset is not None:
            payload["offset"] = offset
        result = self._post("getUpdates", payload)
        if not isinstance(result, list):
            return []
        return [item for item in result if isinstance(item, dict)]

    def send_message(self, *, chat_id: str, text: str) -> None:
        self._post(
            "sendMessage",
            {
                "chat_id": chat_id,
                "text": text,
            },
        )


class _CodexEventValidationFilter(logging.Filter):
    """Emit selected `codex/event` notifications and suppress warning noise."""

    @staticmethod
    def _extract_notification_payload(message: str) -> str:
        marker = "Message was:"
        if marker in message:
            payload = message.split(marker, 1)[1].strip()
            if payload:
                return payload
        return message

    @staticmethod
    def _extract_params_literal(payload: str) -> str | None:
        marker = "params="
        marker_index = payload.find(marker)
        if marker_index < 0:
            return None

        raw = payload[marker_index + len(marker) :]
        start = raw.find("{")
        if start < 0:
            return None

        depth = 0
        in_single = False
        in_double = False
        escaped = False
        started = False

        for index, char in enumerate(raw[start:], start=start):
            if escaped:
                escaped = False
                continue
            if char == "\\":
                escaped = True
                continue

            if char == "'" and not in_double:
                in_single = not in_single
                continue
            if char == '"' and not in_single:
                in_double = not in_double
                continue
            if in_single or in_double:
                continue

            if char == "{":
                depth += 1
                started = True
                continue
            if char == "}":
                depth -= 1
                if started and depth == 0:
                    return raw[start : index + 1]

        return None

    @classmethod
    def _extract_agent_text_notification(cls, message: str) -> _AgentTextNotification | None:
        payload = cls._extract_notification_payload(message)
        if "method='codex/event'" not in payload:
            return None

        params_literal = cls._extract_params_literal(payload)
        if params_literal is None:
            return None

        try:
            params = ast.literal_eval(params_literal)
        except Exception:
            return None
        if not isinstance(params, dict):
            return None

        msg = params.get("msg")
        if not isinstance(msg, dict):
            return None
        if msg.get("type") != "item_completed":
            return None

        item = msg.get("item")
        if not isinstance(item, dict):
            return None
        if item.get("type") != "AgentMessage":
            return None

        phase = item.get("phase")
        if not isinstance(phase, str) or not phase:
            return None

        content = item.get("content")
        if not isinstance(content, list):
            return None

        text_parts: list[str] = []
        for content_item in content:
            if not isinstance(content_item, dict):
                continue
            if content_item.get("type") != "Text":
                continue
            text = content_item.get("text")
            if isinstance(text, str) and text:
                text_parts.append(text)

        if not text_parts:
            return None

        message_id = item.get("id")
        return _AgentTextNotification(
            message_id=message_id if isinstance(message_id, str) else "",
            phase=phase,
            text="\n".join(text_parts),
        )

    @staticmethod
    def _format_agent_text_notification(notification: _AgentTextNotification) -> str:
        return (
            "[codex-notification] "
            f"id={notification.message_id} "
            f"phase={notification.phase} "
            f"text={notification.text}"
        )

    @staticmethod
    def _dispatch_intermediate_notification(notification: _AgentTextNotification) -> None:
        if notification.phase == "final_answer":
            return

        callback = _ACTIVE_NOTIFICATION_HANDLER.get()
        if callback is None:
            callback = _ACTIVE_NOTIFICATION_HANDLER_FALLBACK
        if callback is None:
            return

        try:
            callback(notification)
        except Exception as exc:
            _stdout_print(f"[warn] failed to dispatch codex notification: {exc}")

    def filter(self, record: logging.LogRecord) -> bool:
        message = record.getMessage()
        if "Failed to validate notification" in message and "codex/event" in message:
            notification = self._extract_agent_text_notification(message)
            if notification is not None:
                _stdout_print(self._format_agent_text_notification(notification), flush=True)
                self._dispatch_intermediate_notification(notification)
            return False
        return True


def _configure_logging() -> None:
    logging.getLogger().addFilter(_CodexEventValidationFilter())


def _resolve_conf_path(raw_path: str | Path) -> Path:
    path = Path(raw_path).expanduser()
    if not path.is_absolute():
        path = Path.cwd() / path
    return path.resolve()


def _ensure_conf_exists(path: Path) -> None:
    if path.exists():
        return
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(_DEFAULT_CONF_TEMPLATE, encoding="utf-8")
    except OSError as exc:
        raise ValueError(f"failed to create default conf file at {path}: {exc}") from exc


def _load_toml_payload(conf_path: Path) -> dict[str, Any]:
    try:
        import tomllib
    except Exception as exc:
        raise RuntimeError("Python 3.11+ is required for conf.toml parsing (tomllib).") from exc

    try:
        payload = tomllib.loads(conf_path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise ValueError(f"failed to parse {conf_path}: {exc}") from exc

    if not isinstance(payload, dict):
        raise ValueError(f"{conf_path}: root must be a table")
    return payload


def _optional_bool(*, value: Any, conf_path: Path, key_name: str, default: bool) -> bool:
    if value is None:
        return default
    if not isinstance(value, bool):
        raise ValueError(f"{conf_path}: {key_name} must be a boolean")
    return value


def _optional_positive_int(*, value: Any, conf_path: Path, key_name: str, default: int) -> int:
    if value is None:
        return default
    if not isinstance(value, int) or value <= 0:
        raise ValueError(f"{conf_path}: {key_name} must be a positive integer")
    return value


def _optional_positive_float(*, value: Any, conf_path: Path, key_name: str, default: float) -> float:
    if value is None:
        return default
    if not isinstance(value, (int, float)) or value <= 0:
        raise ValueError(f"{conf_path}: {key_name} must be a positive number")
    return float(value)


def _parse_id_allowlist(
    *,
    value: Any,
    conf_path: Path,
    key_name: str,
    allow_csv_string: bool,
) -> set[str] | None:
    if value is None:
        return None

    if allow_csv_string and isinstance(value, str):
        normalized = {piece.strip() for piece in value.split(",") if piece.strip()}
        return normalized or None

    if not isinstance(value, list):
        raise ValueError(f"{conf_path}: {key_name} must be a list")

    parsed: set[str] = set()
    for item in value:
        if isinstance(item, (int, str)):
            normalized = str(item).strip()
            if normalized:
                parsed.add(normalized)
            continue
        raise ValueError(f"{conf_path}: {key_name} supports only int/string items")
    return parsed or None


def _parse_allowed_users_from_payload(*, payload: dict[str, Any], conf_path: Path) -> set[str] | None:
    telegram = payload.get("telegram")
    if telegram is None:
        return None
    if not isinstance(telegram, dict):
        raise ValueError(f"{conf_path}: [telegram] must be a table")
    return _parse_id_allowlist(
        value=telegram.get("allowed_users"),
        conf_path=conf_path,
        key_name="telegram.allowed_users",
        allow_csv_string=False,
    )


def _parse_polling_config_from_payload(
    *,
    payload: dict[str, Any],
    conf_path: Path,
) -> _PollingConfig:
    telegram = payload.get("telegram")
    if telegram is None:
        return _PollingConfig()
    if not isinstance(telegram, dict):
        raise ValueError(f"{conf_path}: [telegram] must be a table")

    polling = telegram.get("polling")
    if polling is None:
        return _PollingConfig()
    if not isinstance(polling, dict):
        raise ValueError(f"{conf_path}: [telegram.polling] must be a table")

    return _PollingConfig(
        poll_timeout=_optional_positive_int(
            value=polling.get("poll_timeout"),
            conf_path=conf_path,
            key_name="telegram.polling.poll_timeout",
            default=30,
        ),
        loop_sleep_sec=_optional_positive_float(
            value=polling.get("loop_sleep_sec"),
            conf_path=conf_path,
            key_name="telegram.polling.loop_sleep_sec",
            default=1.0,
        ),
        delete_webhook_on_start=_optional_bool(
            value=polling.get("delete_webhook_on_start"),
            conf_path=conf_path,
            key_name="telegram.polling.delete_webhook_on_start",
            default=True,
        ),
        drop_pending_updates=_optional_bool(
            value=polling.get("drop_pending_updates"),
            conf_path=conf_path,
            key_name="telegram.polling.drop_pending_updates",
            default=False,
        ),
        ignore_pending_updates_on_start=_optional_bool(
            value=polling.get("ignore_pending_updates_on_start"),
            conf_path=conf_path,
            key_name="telegram.polling.ignore_pending_updates_on_start",
            default=True,
        ),
        require_mcp_warmup=_optional_bool(
            value=polling.get("require_mcp_warmup"),
            conf_path=conf_path,
            key_name="telegram.polling.require_mcp_warmup",
            default=True,
        ),
        cancel_wait_timeout_sec=_optional_positive_float(
            value=polling.get("cancel_wait_timeout_sec"),
            conf_path=conf_path,
            key_name="telegram.polling.cancel_wait_timeout_sec",
            default=5.0,
        ),
    )


def _load_runner_config_from_conf(conf_path: str) -> tuple[Path, _RunnerConfig]:
    path = _resolve_conf_path(conf_path)
    _ensure_conf_exists(path)
    payload = _load_toml_payload(path)
    return path, _RunnerConfig(
        allowed_users=_parse_allowed_users_from_payload(payload=payload, conf_path=path),
        polling=_parse_polling_config_from_payload(payload=payload, conf_path=path),
    )


def _load_allowed_users_from_conf(conf_path: str) -> set[str] | None:
    _, runner_conf = _load_runner_config_from_conf(conf_path)
    return runner_conf.allowed_users


def _safe_send(api: TelegramBotApi, chat_id: str, text: str) -> None:
    for chunk in split_telegram_text(text):
        try:
            api.send_message(chat_id=chat_id, text=chunk)
        except Exception as exc:
            _stdout_print(f"[warn] failed to send telegram message: {exc}")


async def _run_blocking(func: Any, /, *args: Any, **kwargs: Any) -> Any:
    loop = asyncio.get_running_loop()
    bound = functools.partial(func, *args, **kwargs)
    return await loop.run_in_executor(_BLOCKING_POOL, bound)


def _next_offset_from_updates(updates: list[dict[str, Any]]) -> int | None:
    latest_update_id: int | None = None
    for update in updates:
        update_id = update.get("update_id")
        if not isinstance(update_id, int):
            continue
        if latest_update_id is None or update_id > latest_update_id:
            latest_update_id = update_id

    if latest_update_id is None:
        return None
    return latest_update_id + 1


def _render_progress_message(
    *,
    template: str,
    elapsed_sec: int,
    progress_count: int,
) -> str:
    try:
        rendered = template.format(elapsed_sec=elapsed_sec, progress_count=progress_count).strip()
        if rendered:
            return rendered
    except Exception:
        pass
    return f"still working... elapsed={elapsed_sec}s"


def _is_cancel_command(text: str | None) -> bool:
    raw = (text or "").strip()
    if not raw.startswith("/"):
        return False

    command = raw.split(maxsplit=1)[0].lower()
    return command == "/cancel" or command.startswith("/cancel@")


def _format_inbound_stdout(*, chat_id: str, user_id: str, text: str) -> str:
    escaped_text = text.replace("\r", "\\r").replace("\n", "\\n")
    return f"[telegram-inbound] chat_id={chat_id} user_id={user_id} text={escaped_text}"


async def _run_with_progress_notifications(
    *,
    orchestrator: Any,
    api: TelegramBotApi,
    chat_id: str,
    user_id: str,
    text: str,
    enabled: bool,
    initial_delay_sec: float,
    interval_sec: float,
    message_template: str,
) -> str:
    del enabled, initial_delay_sec, interval_sec, message_template
    loop = asyncio.get_running_loop()

    def _forward_intermediate(notification: _AgentTextNotification) -> None:
        async def _send() -> None:
            try:
                await _run_blocking(_safe_send, api, chat_id, notification.text)
            except Exception as exc:
                _stdout_print(f"[warn] failed to forward intermediate codex notification: {exc}")

        try:
            running_loop = asyncio.get_running_loop()
        except RuntimeError:
            asyncio.run_coroutine_threadsafe(_send(), loop)
            return

        if running_loop is loop:
            loop.create_task(_send())
            return

        asyncio.run_coroutine_threadsafe(_send(), loop)

    global _ACTIVE_NOTIFICATION_HANDLER_FALLBACK
    callback_token = _ACTIVE_NOTIFICATION_HANDLER.set(_forward_intermediate)
    _ACTIVE_NOTIFICATION_HANDLER_FALLBACK = _forward_intermediate

    try:
        return await orchestrator.handle_message(chat_id, user_id, text)
    finally:
        _ACTIVE_NOTIFICATION_HANDLER.reset(callback_token)
        if _ACTIVE_NOTIFICATION_HANDLER_FALLBACK is _forward_intermediate:
            _ACTIVE_NOTIFICATION_HANDLER_FALLBACK = None


async def _process_inbound_request(
    *,
    orchestrator: Any,
    api: TelegramBotApi,
    chat_id: str,
    user_id: str,
    text: str,
    progress_notify: bool,
    progress_initial_delay_sec: float,
    progress_interval_sec: float,
    progress_message_template: str,
) -> None:
    try:
        output = await _run_with_progress_notifications(
            orchestrator=orchestrator,
            api=api,
            chat_id=chat_id,
            user_id=user_id,
            text=text,
            enabled=progress_notify,
            initial_delay_sec=progress_initial_delay_sec,
            interval_sec=progress_interval_sec,
            message_template=progress_message_template,
        )
    except Exception as exc:
        output = f"internal error: {exc}"
    finally:
        try:
            await _close_codex_mcp(orchestrator)
        except Exception as exc:
            _stdout_print(f"[warn] failed to close codex mcp session after request: {exc}")

    await _run_blocking(_safe_send, api, chat_id, output)


async def _cancel_inflight_request(
    *,
    orchestrator: Any,
    request_task: asyncio.Task[None] | None,
) -> bool:
    del orchestrator
    if request_task is None:
        return False
    if request_task.done():
        try:
            request_task.result()
        except asyncio.CancelledError:
            pass
        except Exception as exc:
            _stdout_print(f"[warn] request task finished with error before cancel: {exc}")
        return False

    request_task.cancel()
    try:
        await request_task
    except asyncio.CancelledError:
        pass
    except Exception as exc:
        _stdout_print(f"[warn] request task failed while cancelling: {exc}")
    return True


async def _wait_for_request_completion(
    *,
    request_task: asyncio.Task[None] | None,
    timeout_sec: float,
) -> bool:
    if request_task is None:
        return True

    if request_task.done():
        try:
            request_task.result()
        except asyncio.CancelledError:
            pass
        except Exception as exc:
            _stdout_print(f"[warn] request task failed after cancel: {exc}")
        return True

    try:
        await asyncio.wait_for(asyncio.shield(request_task), timeout=timeout_sec)
        return True
    except asyncio.TimeoutError:
        return False
    except asyncio.CancelledError:
        return True
    except Exception as exc:
        _stdout_print(f"[warn] request task failed after cancel: {exc}")
        return True


def _extract_executor(orchestrator: Any) -> Any | None:
    single_workflow = getattr(orchestrator, "single_workflow", None)
    developer = getattr(single_workflow, "developer", None)
    executor = getattr(developer, "_executor", None)
    if executor is not None:
        return executor

    multi_workflow = getattr(orchestrator, "multi_workflow", None)
    return getattr(multi_workflow, "executor", None)


def _format_mcp_status(status: dict[str, Any]) -> str:
    running = status.get("running")
    ready = status.get("ready")
    pid = status.get("pid")
    uptime = status.get("uptime_sec")
    uptime_text = "-" if uptime is None else f"{uptime}s"
    return f"running={running}, ready={ready}, pid={pid}, uptime={uptime_text}"


async def _warmup_codex_mcp(orchestrator: Any) -> bool:
    executor = _extract_executor(orchestrator)
    if isinstance(executor, EchoCodexExecutor):
        _stdout_print("[warn] codex.allow_echo_executor=true (debug mode). mcp warmup is skipped.")
        return False

    if not isinstance(executor, CodexMcpExecutor):
        _stdout_print(f"[warn] unknown executor type: {type(executor).__name__}; skip mcp warmup")
        return False

    try:
        await executor.warmup()
        status = orchestrator.codex_mcp.get_status()
        _stdout_print(f"[info] codex mcp-server connected: {_format_mcp_status(status)}")
        # Keep request task affinity stable by not retaining an opened MCP session
        # from startup task. Each inbound request opens/closes its own session.
        await executor.close()
        return True
    except Exception as exc:
        _stdout_print(f"[error] codex mcp-server connection failed: {exc}")
        return False


async def _close_codex_mcp(orchestrator: Any) -> None:
    executor = _extract_executor(orchestrator)
    if not isinstance(executor, CodexMcpExecutor):
        return

    try:
        await executor.close()
    except Exception as exc:
        _stdout_print(f"[warn] failed to close codex mcp session: {exc}")


async def _run_polling() -> None:
    conf_path = os.getenv("CODEX_CONF_PATH", str(_DEFAULT_CONF_PATH)).strip() or str(_DEFAULT_CONF_PATH)
    try:
        conf_file, runner_conf = _load_runner_config_from_conf(conf_path)
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc

    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    if not token:
        raise SystemExit("TELEGRAM_BOT_TOKEN is required")

    allowed_users = runner_conf.allowed_users
    polling = runner_conf.polling
    poll_timeout = polling.poll_timeout
    loop_sleep_sec = polling.loop_sleep_sec
    clear_webhook = polling.delete_webhook_on_start
    drop_pending = polling.drop_pending_updates
    ignore_pending_updates_on_start = polling.ignore_pending_updates_on_start
    cancel_wait_timeout_sec = polling.cancel_wait_timeout_sec
    require_mcp_warmup = polling.require_mcp_warmup

    # Reserved for compatibility; synthetic progress messages are currently disabled.
    progress_notify = True
    progress_initial_delay_sec = 15.0
    progress_interval_sec = 20.0
    progress_message_template = "still working... elapsed={elapsed_sec}s"

    api = TelegramBotApi(token=token)
    _stdout_print(f"[info] conf file: {conf_file}")
    if allowed_users is not None:
        _stdout_print(f"[info] telegram user allowlist enabled: count={len(allowed_users)}")
    else:
        _stdout_print("[info] telegram user allowlist disabled (telegram.allowed_users not set)")

    if clear_webhook:
        try:
            await _run_blocking(api.delete_webhook, drop_pending_updates=drop_pending)
        except Exception as exc:
            _stdout_print(f"[warn] failed to delete webhook on startup: {exc}")

    orchestrator = build_orchestrator()
    active_request_task: asyncio.Task[None] | None = None
    try:
        next_offset: int | None = None
        if ignore_pending_updates_on_start:
            try:
                pending_updates = await _run_blocking(api.get_updates, offset=None, timeout=0)
                next_offset = _next_offset_from_updates(pending_updates)
                if next_offset is not None:
                    _stdout_print(
                        "[info] skipped pending telegram updates on startup: "
                        f"count={len(pending_updates)}, next_offset={next_offset}"
                    )
            except Exception as exc:
                _stdout_print(f"[warn] failed to skip pending telegram updates on startup: {exc}")

        warmup_ok = await _warmup_codex_mcp(orchestrator)
        if require_mcp_warmup and not warmup_ok:
            raise SystemExit(
                "codex mcp-server warmup failed. "
                "Check conf.toml [codex].mcp_command/[codex].mcp_args and runtime auth settings, "
                "or disable strict check with [telegram.polling].require_mcp_warmup=false"
            )

        _stdout_print("[info] telegram polling runner started")
        while True:
            if active_request_task is not None and active_request_task.done():
                try:
                    active_request_task.result()
                except asyncio.CancelledError:
                    pass
                except Exception as exc:
                    _stdout_print(f"[warn] request task failed: {exc}")
                active_request_task = None

            try:
                updates = await _run_blocking(
                    api.get_updates,
                    offset=next_offset,
                    timeout=poll_timeout,
                )
                for update in updates:
                    update_id = update.get("update_id")
                    if isinstance(update_id, int):
                        next_offset = update_id + 1

                    inbound = parse_update(update)
                    if not inbound:
                        continue

                    _stdout_print(
                        _format_inbound_stdout(
                            chat_id=inbound.chat_id,
                            user_id=inbound.user_id,
                            text=inbound.text,
                        ),
                        flush=True,
                    )

                    if allowed_users is not None and inbound.user_id not in allowed_users:
                        await _run_blocking(
                            _safe_send,
                            api,
                            inbound.chat_id,
                            _UNAUTHORIZED_MESSAGE,
                        )
                        continue

                    if _is_cancel_command(inbound.text):
                        try:
                            output = await orchestrator.handle_message(
                                inbound.chat_id,
                                inbound.user_id,
                                "/cancel",
                            )
                        except Exception as exc:
                            output = f"internal error: {exc}"

                        if active_request_task is not None:
                            normalized_output = output.strip().lower()
                            # Primary cancel path is routed through orchestrator.
                            # Fallback to direct task cancel only when orchestrator
                            # reports no running task but one is still active here.
                            if normalized_output == "no running task to cancel.":
                                await _cancel_inflight_request(
                                    orchestrator=orchestrator,
                                    request_task=active_request_task,
                                )
                            else:
                                completed = await _wait_for_request_completion(
                                    request_task=active_request_task,
                                    timeout_sec=cancel_wait_timeout_sec,
                                )
                                if not completed:
                                    _stdout_print(
                                        "[warn] cancel acknowledged but request is still shutting down"
                                    )
                            if active_request_task.done():
                                active_request_task = None

                        await _run_blocking(_safe_send, api, inbound.chat_id, output)
                        continue

                    if active_request_task is not None and not active_request_task.done():
                        await _run_blocking(
                            _safe_send,
                            api,
                            inbound.chat_id,
                            "A task is already running for this session. Please try again shortly.",
                        )
                        continue

                    active_request_task = asyncio.create_task(
                        _process_inbound_request(
                            orchestrator=orchestrator,
                            api=api,
                            chat_id=inbound.chat_id,
                            user_id=inbound.user_id,
                            text=inbound.text,
                            progress_notify=progress_notify,
                            progress_initial_delay_sec=progress_initial_delay_sec,
                            progress_interval_sec=progress_interval_sec,
                            progress_message_template=progress_message_template,
                        )
                    )
            except Exception as exc:
                _stdout_print(f"[warn] polling loop error: {exc}")

            if loop_sleep_sec > 0:
                await asyncio.sleep(loop_sleep_sec)
    finally:
        if active_request_task is not None and not active_request_task.done():
            active_request_task.cancel()
            try:
                await active_request_task
            except asyncio.CancelledError:
                pass
            except Exception:
                pass
        await _close_codex_mcp(orchestrator)


def main() -> None:
    _configure_logging()
    try:
        asyncio.run(_run_polling())
    except KeyboardInterrupt:
        _stdout_print("\n[info] stopped by user")


if __name__ == "__main__":
    main()
