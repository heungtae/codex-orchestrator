from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Literal, TypedDict

BotMode = Literal["single", "multi"]
InputKind = Literal["bot_command", "codex_slash", "text"]
BotCommandName = Literal["start", "mode", "new", "status", "profile", "cancel"]
ReviewResult = Literal["approved", "needs_changes", "max_rounds_reached"]
RunStatus = Literal["idle", "ok", "error"]


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass(frozen=True)
class RouteResult:
    kind: InputKind
    text: str
    command: BotCommandName | None = None
    args: tuple[str, ...] = ()


@dataclass
class BotSession:
    session_id: str
    chat_id: str
    user_id: str
    mode: BotMode = "single"
    history: list[dict[str, Any]] = field(default_factory=list)
    run_lock: bool = False
    last_error: str | None = None
    last_run_status: RunStatus = "idle"
    last_run_latency_ms: int | None = None
    last_review_round: int = 0
    last_review_result: ReviewResult | None = None
    profile_name: str = "default"
    profile_model: str | None = None
    profile_working_directory: str | None = None
    profile_agent_models: dict[str, str] = field(default_factory=dict)
    profile_agent_system_prompts: dict[str, str] = field(default_factory=dict)
    updated_at: str = field(default_factory=utc_now_iso)

    def touch(self) -> None:
        self.updated_at = utc_now_iso()

    def to_dict(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "chat_id": self.chat_id,
            "user_id": self.user_id,
            "mode": self.mode,
            "history": self.history,
            "run_lock": self.run_lock,
            "last_error": self.last_error,
            "last_run_status": self.last_run_status,
            "last_run_latency_ms": self.last_run_latency_ms,
            "last_review_round": self.last_review_round,
            "last_review_result": self.last_review_result,
            "profile_name": self.profile_name,
            "profile_model": self.profile_model,
            "profile_working_directory": self.profile_working_directory,
            "profile_agent_models": self.profile_agent_models,
            "profile_agent_system_prompts": self.profile_agent_system_prompts,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "BotSession":
        mode = payload.get("mode", "single")
        if mode not in ("single", "multi"):
            mode = "single"

        last_run_status = payload.get("last_run_status", "idle")
        if last_run_status not in ("idle", "ok", "error"):
            last_run_status = "idle"

        last_review_result = payload.get("last_review_result")
        if last_review_result not in ("approved", "needs_changes", "max_rounds_reached", None):
            last_review_result = None

        history = payload.get("history", [])
        if not isinstance(history, list):
            history = []

        profile_name = str(payload.get("profile_name", "default") or "default").strip() or "default"
        profile_model = payload.get("profile_model")
        if profile_model is not None:
            profile_model = str(profile_model).strip() or None
        profile_working_directory = payload.get("profile_working_directory")
        if profile_working_directory is not None:
            profile_working_directory = str(profile_working_directory).strip() or None
        profile_agent_models = _parse_string_map(payload.get("profile_agent_models"))
        profile_agent_system_prompts = _parse_string_map(payload.get("profile_agent_system_prompts"))

        return cls(
            session_id=str(payload["session_id"]),
            chat_id=str(payload["chat_id"]),
            user_id=str(payload["user_id"]),
            mode=mode,
            history=history,
            run_lock=bool(payload.get("run_lock", False)),
            last_error=payload.get("last_error"),
            last_run_status=last_run_status,
            last_run_latency_ms=payload.get("last_run_latency_ms"),
            last_review_round=int(payload.get("last_review_round", 0) or 0),
            last_review_result=last_review_result,
            profile_name=profile_name,
            profile_model=profile_model,
            profile_working_directory=profile_working_directory,
            profile_agent_models=profile_agent_models,
            profile_agent_system_prompts=profile_agent_system_prompts,
            updated_at=str(payload.get("updated_at", utc_now_iso())),
        )


def _parse_string_map(value: Any) -> dict[str, str]:
    if not isinstance(value, dict):
        return {}

    parsed: dict[str, str] = {}
    for raw_key, raw_val in value.items():
        key = str(raw_key).strip().lower()
        if not key:
            continue
        if raw_val is None:
            continue
        cleaned = str(raw_val).strip()
        if not cleaned:
            continue
        parsed[key] = cleaned
    return parsed


class WorkflowResult(TypedDict, total=False):
    output_text: str
    next_history: list[dict[str, Any]]
    review_round: int
    review_result: ReviewResult
    metadata: dict[str, Any]


class TraceRecord(TypedDict, total=False):
    timestamp: str
    run_id: str
    session_id: str
    mode: BotMode
    review_round: int
    review_result: ReviewResult
    input_kind: InputKind
    input_text: str
    output_text: str
    status: Literal["ok", "error"]
    latency_ms: int
    error_message: str
