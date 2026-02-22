#!/usr/bin/env python3
from __future__ import annotations

import asyncio
import functools
import json
import logging
import os
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
)
_UNAUTHORIZED_MESSAGE = "Unauthorized"


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
    """Suppress known MCP notification schema noise from `codex/event`."""

    def filter(self, record: logging.LogRecord) -> bool:
        message = record.getMessage()
        if "Failed to validate notification" in message and "codex/event" in message:
            return False
        return True


def _configure_logging() -> None:
    logging.getLogger().addFilter(_CodexEventValidationFilter())


def _parse_allowed_chat_ids(raw: str) -> set[str] | None:
    value = raw.strip()
    if not value:
        return None
    return {piece.strip() for piece in value.split(",") if piece.strip()}


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


def _load_allowed_users_from_conf(conf_path: str) -> set[str] | None:
    path = _resolve_conf_path(conf_path)
    _ensure_conf_exists(path)

    try:
        import tomllib
    except Exception as exc:
        raise RuntimeError("Python 3.11+ is required for conf.toml parsing (tomllib).") from exc

    try:
        payload = tomllib.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise ValueError(f"failed to parse {path}: {exc}") from exc

    telegram = payload.get("telegram")
    if telegram is None:
        return None
    if not isinstance(telegram, dict):
        raise ValueError(f"{path}: [telegram] must be a table")

    allowed_users = telegram.get("allowed_users")
    if allowed_users is None:
        return None
    if not isinstance(allowed_users, list):
        raise ValueError(f"{path}: telegram.allowed_users must be a list")

    parsed: set[str] = set()
    for item in allowed_users:
        if isinstance(item, (int, str)):
            user_id = str(item).strip()
            if user_id:
                parsed.add(user_id)
            continue
        raise ValueError(f"{path}: telegram.allowed_users supports only int/string items")

    return parsed


def _env_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    normalized = value.strip().lower()
    return normalized in {"1", "true", "yes", "y", "on"}


def _env_float(name: str, default: float) -> float:
    value = os.getenv(name)
    if value is None:
        return default

    raw = value.strip()
    if not raw:
        return default

    try:
        parsed = float(raw)
    except ValueError:
        return default

    if parsed <= 0:
        return default
    return parsed


def _safe_send(api: TelegramBotApi, chat_id: str, text: str) -> None:
    for chunk in split_telegram_text(text):
        try:
            api.send_message(chat_id=chat_id, text=chunk)
        except Exception as exc:
            print(f"[warn] failed to send telegram message: {exc}")


async def _run_blocking(func: Any, /, *args: Any, **kwargs: Any) -> Any:
    loop = asyncio.get_running_loop()
    bound = functools.partial(func, *args, **kwargs)
    return await loop.run_in_executor(_BLOCKING_POOL, bound)


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
    run_task = asyncio.create_task(orchestrator.handle_message(chat_id, user_id, text))
    if not enabled:
        return await run_task

    started = time.monotonic()
    progress_count = 0

    while True:
        timeout = initial_delay_sec if progress_count == 0 else interval_sec
        try:
            return await asyncio.wait_for(asyncio.shield(run_task), timeout=timeout)
        except asyncio.TimeoutError:
            progress_count += 1
            elapsed_sec = int(time.monotonic() - started)
            progress_message = _render_progress_message(
                template=message_template,
                elapsed_sec=elapsed_sec,
                progress_count=progress_count,
            )
            await _run_blocking(_safe_send, api, chat_id, progress_message)


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
        print("[warn] CODEX_ALLOW_ECHO_EXECUTOR=true (debug mode). mcp warmup is skipped.")
        return False

    if not isinstance(executor, CodexMcpExecutor):
        print(f"[warn] unknown executor type: {type(executor).__name__}; skip mcp warmup")
        return False

    try:
        await executor.warmup()
        status = orchestrator.codex_mcp.get_status()
        print(f"[info] codex mcp-server connected: {_format_mcp_status(status)}")
        return True
    except Exception as exc:
        print(f"[error] codex mcp-server connection failed: {exc}")
        return False


async def _close_codex_mcp(orchestrator: Any) -> None:
    executor = _extract_executor(orchestrator)
    if not isinstance(executor, CodexMcpExecutor):
        return

    try:
        await executor.close()
    except Exception as exc:
        print(f"[warn] failed to close codex mcp session: {exc}")


async def _run_polling() -> None:
    conf_path = os.getenv("CODEX_CONF_PATH", str(_DEFAULT_CONF_PATH)).strip() or str(_DEFAULT_CONF_PATH)
    conf_file = _resolve_conf_path(conf_path)
    try:
        allowed_users = _load_allowed_users_from_conf(str(conf_file))
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc

    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    if not token:
        raise SystemExit("TELEGRAM_BOT_TOKEN is required")

    poll_timeout = int(os.getenv("TELEGRAM_POLL_TIMEOUT", "30"))
    loop_sleep_sec = float(os.getenv("TELEGRAM_LOOP_SLEEP_SEC", "1"))
    clear_webhook = _env_bool("TELEGRAM_DELETE_WEBHOOK_ON_START", True)
    drop_pending = _env_bool("TELEGRAM_DROP_PENDING_UPDATES", False)
    allowed_chat_ids = _parse_allowed_chat_ids(os.getenv("TELEGRAM_ALLOWED_CHAT_IDS", ""))

    progress_notify = _env_bool("TELEGRAM_PROGRESS_NOTIFY", True)
    progress_initial_delay_sec = _env_float("TELEGRAM_PROGRESS_INITIAL_DELAY_SEC", 15.0)
    progress_interval_sec = _env_float("TELEGRAM_PROGRESS_INTERVAL_SEC", 20.0)
    progress_message_template = os.getenv(
        "TELEGRAM_PROGRESS_MESSAGE",
        "still working... elapsed={elapsed_sec}s",
    )

    api = TelegramBotApi(token=token)
    print(f"[info] conf file: {conf_file}")
    if allowed_users is not None:
        print(f"[info] telegram user allowlist enabled: count={len(allowed_users)}")
    else:
        print("[info] telegram user allowlist disabled (telegram.allowed_users not set)")

    if clear_webhook:
        try:
            await _run_blocking(api.delete_webhook, drop_pending_updates=drop_pending)
        except Exception as exc:
            print(f"[warn] failed to delete webhook on startup: {exc}")

    orchestrator = build_orchestrator()
    try:
        require_mcp_warmup = _env_bool("TELEGRAM_REQUIRE_MCP_WARMUP", True)
        warmup_ok = await _warmup_codex_mcp(orchestrator)
        if require_mcp_warmup and not warmup_ok:
            raise SystemExit(
                "codex mcp-server warmup failed. "
                "Check CODEX_MCP_COMMAND/CODEX_MCP_ARGS and runtime auth settings, "
                "or disable strict check with TELEGRAM_REQUIRE_MCP_WARMUP=false"
            )
        next_offset: int | None = None

        print("[info] telegram polling runner started")
        while True:
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

                    if allowed_users is not None and inbound.user_id not in allowed_users:
                        await _run_blocking(
                            _safe_send,
                            api,
                            inbound.chat_id,
                            _UNAUTHORIZED_MESSAGE,
                        )
                        continue

                    if allowed_chat_ids is not None and inbound.chat_id not in allowed_chat_ids:
                        await _run_blocking(_safe_send, api, inbound.chat_id, "not allowed chat")
                        continue

                    try:
                        output = await _run_with_progress_notifications(
                            orchestrator=orchestrator,
                            api=api,
                            chat_id=inbound.chat_id,
                            user_id=inbound.user_id,
                            text=inbound.text,
                            enabled=progress_notify,
                            initial_delay_sec=progress_initial_delay_sec,
                            interval_sec=progress_interval_sec,
                            message_template=progress_message_template,
                        )
                    except Exception as exc:
                        output = f"internal error: {exc}"

                    await _run_blocking(_safe_send, api, inbound.chat_id, output)
            except Exception as exc:
                print(f"[warn] polling loop error: {exc}")

            if loop_sleep_sec > 0:
                await asyncio.sleep(loop_sleep_sec)
    finally:
        await _close_codex_mcp(orchestrator)


def main() -> None:
    _configure_logging()
    try:
        asyncio.run(_run_polling())
    except KeyboardInterrupt:
        print("\n[info] stopped by user")


if __name__ == "__main__":
    main()
