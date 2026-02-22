from __future__ import annotations

import os
import shlex

from core.command_router import CommandRouter
from core.orchestrator import BotOrchestrator
from core.session_manager import SessionManager
from core.trace_logger import TraceLogger
from integrations.codex_executor import (
    CodexMcpExecutor,
    EchoCodexExecutor,
)
from integrations.codex_mcp import CodexMcpServer
from workflows.agent_factory import AgentFactory

try:  # Optional dependency; keep runtime usable without dotenv.
    from dotenv import load_dotenv

    load_dotenv(override=True)
except Exception:
    pass


def _env_truthy(name: str) -> bool:
    value = os.getenv(name, "")
    return value.strip().lower() in {"1", "true", "yes", "y", "on"}


def _env_int(name: str, default: int) -> int:
    value = os.getenv(name, "").strip()
    if not value:
        return default
    try:
        return int(value)
    except ValueError:
        return default


def build_orchestrator() -> BotOrchestrator:
    mcp_command = os.getenv("CODEX_MCP_COMMAND", "npx").strip() or "npx"
    mcp_args_raw = os.getenv("CODEX_MCP_ARGS", "-y codex mcp-server").strip()
    mcp_args = tuple(shlex.split(mcp_args_raw)) if mcp_args_raw else ("-y", "codex", "mcp-server")
    mcp_timeout_sec = _env_int("CODEX_MCP_CLIENT_TIMEOUT_SECONDS", 360000)
    codex_model = os.getenv("CODEX_AGENT_MODEL", "").strip() or None
    status_command_raw = os.getenv("CODEX_MCP_STATUS_CMD", "").strip()
    allow_echo_executor = _env_truthy("CODEX_ALLOW_ECHO_EXECUTOR")
    status_command = shlex.split(status_command_raw) if status_command_raw else None
    codex_mcp = CodexMcpServer(status_command=status_command)

    if allow_echo_executor:
        # Explicitly gated for local debugging only.
        executor = EchoCodexExecutor()
    else:
        executor = CodexMcpExecutor(
            mcp_command=mcp_command,
            mcp_args=mcp_args,
            client_session_timeout_seconds=mcp_timeout_sec,
            default_model=codex_model,
            status_tracker=codex_mcp,
        )

    agent_factory = AgentFactory(executor=executor, max_review_rounds=3)
    return BotOrchestrator(
        router=CommandRouter(),
        session_manager=SessionManager(),
        trace_logger=TraceLogger(),
        single_workflow=agent_factory.create_single_workflow(),
        multi_workflow=agent_factory.create_multi_workflow(),
        codex_mcp=codex_mcp,
    )
