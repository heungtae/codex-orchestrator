from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import shlex
from typing import Any

from core.command_router import CommandRouter
from core.orchestrator import BotOrchestrator
from core.profiles import load_profiles_from_conf, resolve_conf_path
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

_DEFAULT_CONF_PATH = "~/.codex-orchestrator/conf.toml"
_DEFAULT_MCP_ARGS = ("-y", "codex", "mcp-server")


@dataclass(frozen=True)
class _CodexRuntimeConfig:
    mcp_command: str = "npx"
    mcp_args: tuple[str, ...] = _DEFAULT_MCP_ARGS
    mcp_client_timeout_seconds: int = 360000
    agent_model: str | None = None
    agent_working_directory: str | None = None
    allow_echo_executor: bool = False
    approval_policy: str = "never"
    sandbox: str = "danger-full-access"
    mcp_direct_status: bool = True
    mcp_status_cmd: str | None = None
    mcp_auto_detect_process: bool = False


def _load_toml_payload(conf_path: Path) -> dict[str, Any]:
    if not conf_path.exists():
        return {}

    try:
        import tomllib
    except Exception as exc:
        try:
            import tomli as tomllib
        except Exception as tomli_exc:
            raise RuntimeError(
                "TOML parser is required. Use Python 3.11+ or install package `tomli`."
            ) from tomli_exc

    try:
        raw = conf_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ValueError(f"failed to read {conf_path}: {exc}") from exc

    try:
        payload = tomllib.loads(raw)
    except Exception as exc:
        raise ValueError(f"failed to parse {conf_path}: {exc}") from exc

    if not isinstance(payload, dict):
        raise ValueError(f"{conf_path}: root must be a table")
    return payload


def _optional_string(
    *,
    value: Any,
    conf_path: Path,
    key_name: str,
    default: str | None,
) -> str | None:
    if value is None:
        return default
    if not isinstance(value, str):
        raise ValueError(f"{conf_path}: {key_name} must be a string")

    normalized = value.strip()
    if not normalized:
        return default
    return normalized


def _required_bool(
    *,
    value: Any,
    conf_path: Path,
    key_name: str,
    default: bool,
) -> bool:
    if value is None:
        return default
    if not isinstance(value, bool):
        raise ValueError(f"{conf_path}: {key_name} must be a boolean")
    return value


def _required_positive_int(
    *,
    value: Any,
    conf_path: Path,
    key_name: str,
    default: int,
) -> int:
    if value is None:
        return default
    if not isinstance(value, int) or value <= 0:
        raise ValueError(f"{conf_path}: {key_name} must be a positive integer")
    return value


def _resolve_optional_working_directory(
    *,
    conf_path: Path,
    raw_path: str | None,
) -> str | None:
    if raw_path is None:
        return None
    candidate = Path(raw_path).expanduser()
    if not candidate.is_absolute():
        candidate = conf_path.parent / candidate
    return str(candidate.resolve())


def _load_codex_runtime_config(conf_path: Path) -> _CodexRuntimeConfig:
    payload = _load_toml_payload(conf_path)
    raw_codex = payload.get("codex")
    if raw_codex is None:
        return _CodexRuntimeConfig()
    if not isinstance(raw_codex, dict):
        raise ValueError(f"{conf_path}: [codex] must be a table")

    mcp_command = _optional_string(
        value=raw_codex.get("mcp_command"),
        conf_path=conf_path,
        key_name="codex.mcp_command",
        default="npx",
    )
    assert mcp_command is not None

    mcp_args_raw = _optional_string(
        value=raw_codex.get("mcp_args"),
        conf_path=conf_path,
        key_name="codex.mcp_args",
        default="-y codex mcp-server",
    )
    mcp_args = tuple(shlex.split(mcp_args_raw)) if mcp_args_raw else _DEFAULT_MCP_ARGS
    if not mcp_args:
        mcp_args = _DEFAULT_MCP_ARGS

    mcp_client_timeout_seconds = _required_positive_int(
        value=raw_codex.get("mcp_client_timeout_seconds"),
        conf_path=conf_path,
        key_name="codex.mcp_client_timeout_seconds",
        default=360000,
    )
    agent_model = _optional_string(
        value=raw_codex.get("agent_model"),
        conf_path=conf_path,
        key_name="codex.agent_model",
        default=None,
    )
    raw_agent_working_directory = _optional_string(
        value=raw_codex.get("agent_working_directory"),
        conf_path=conf_path,
        key_name="codex.agent_working_directory",
        default=None,
    )
    agent_working_directory = _resolve_optional_working_directory(
        conf_path=conf_path,
        raw_path=raw_agent_working_directory,
    )
    allow_echo_executor = _required_bool(
        value=raw_codex.get("allow_echo_executor"),
        conf_path=conf_path,
        key_name="codex.allow_echo_executor",
        default=False,
    )
    approval_policy = _optional_string(
        value=raw_codex.get("approval_policy"),
        conf_path=conf_path,
        key_name="codex.approval_policy",
        default="never",
    )
    assert approval_policy is not None
    sandbox = _optional_string(
        value=raw_codex.get("sandbox"),
        conf_path=conf_path,
        key_name="codex.sandbox",
        default="danger-full-access",
    )
    assert sandbox is not None
    mcp_direct_status = _required_bool(
        value=raw_codex.get("mcp_direct_status"),
        conf_path=conf_path,
        key_name="codex.mcp_direct_status",
        default=True,
    )
    mcp_status_cmd = _optional_string(
        value=raw_codex.get("mcp_status_cmd"),
        conf_path=conf_path,
        key_name="codex.mcp_status_cmd",
        default=None,
    )
    mcp_auto_detect_process = _required_bool(
        value=raw_codex.get("mcp_auto_detect_process"),
        conf_path=conf_path,
        key_name="codex.mcp_auto_detect_process",
        default=False,
    )

    if mcp_direct_status:
        mcp_status_cmd = None
        mcp_auto_detect_process = False

    return _CodexRuntimeConfig(
        mcp_command=mcp_command,
        mcp_args=mcp_args,
        mcp_client_timeout_seconds=mcp_client_timeout_seconds,
        agent_model=agent_model,
        agent_working_directory=agent_working_directory,
        allow_echo_executor=allow_echo_executor,
        approval_policy=approval_policy,
        sandbox=sandbox,
        mcp_direct_status=mcp_direct_status,
        mcp_status_cmd=mcp_status_cmd,
        mcp_auto_detect_process=mcp_auto_detect_process,
    )


def build_orchestrator() -> BotOrchestrator:
    conf_path_raw = os.getenv("CODEX_CONF_PATH", _DEFAULT_CONF_PATH).strip() or _DEFAULT_CONF_PATH
    conf_path = resolve_conf_path(conf_path_raw)
    codex_config = _load_codex_runtime_config(conf_path)

    status_command = (
        shlex.split(codex_config.mcp_status_cmd) if codex_config.mcp_status_cmd else None
    )

    codex_mcp = CodexMcpServer(
        status_command=status_command,
        auto_detect_process=codex_config.mcp_auto_detect_process,
    )
    profile_registry = load_profiles_from_conf(
        conf_path,
        fallback_model=codex_config.agent_model,
        fallback_working_directory=codex_config.agent_working_directory,
    )
    default_profile = profile_registry.default_profile()

    if codex_config.allow_echo_executor:
        # Explicitly gated for local debugging only.
        executor = EchoCodexExecutor()
    else:
        executor = CodexMcpExecutor(
            mcp_command=codex_config.mcp_command,
            mcp_args=codex_config.mcp_args,
            client_session_timeout_seconds=codex_config.mcp_client_timeout_seconds,
            default_model=default_profile.model,
            status_tracker=codex_mcp,
            approval_policy=codex_config.approval_policy,
            sandbox=codex_config.sandbox,
            cwd=default_profile.working_directory,
        )

    agent_factory = AgentFactory(executor=executor, max_review_rounds=3)
    single_workflow = agent_factory.create_single_workflow()
    plan_workflow = agent_factory.create_plan_workflow(single_workflow=single_workflow)
    return BotOrchestrator(
        router=CommandRouter(),
        session_manager=SessionManager(),
        trace_logger=TraceLogger(),
        single_workflow=single_workflow,
        plan_workflow=plan_workflow,
        multi_workflow=agent_factory.create_multi_workflow(),
        codex_mcp=codex_mcp,
        working_directory=getattr(executor, "cwd", None) or default_profile.working_directory,
        profile_registry=profile_registry,
    )
