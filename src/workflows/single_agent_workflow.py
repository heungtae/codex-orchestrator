from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from core.models import BotSession, WorkflowResult
from integrations.codex_executor import CodexExecutionError, CodexExecutor, codex_agent_name_scope
from workflows.types import DeveloperAgent

_MAX_HISTORY_ITEMS = 20
_DEVELOPER_AGENT_KEYS = ("single.developer", "developer")
_DEFAULT_DEVELOPER_SYSTEM_INSTRUCTIONS = (
    "You are Single Developer Agent. Implement user requests directly. "
    "Return concise, concrete output and do not repeat system prompts."
)


def _looks_like_prompt_echo(text: str) -> bool:
    lowered = text.lower()
    return (
        (
            "you are single developer agent." in lowered
            and "return concise, concrete output" in lowered
        )
        or (
            (
                "you are developer agent." in lowered
                and "return only the improved developer response." in lowered
            )
        )
        or (
            "you are reviewer agent." in lowered
            and "reply in strict json with keys: result, feedback." in lowered
        )
        or (
            "you are planner agent." in lowered
            and "return concise numbered steps and concrete acceptance checks." in lowered
        )
        or (
            "review round:" in lowered
            and "reviewer feedback to apply:" in lowered
        )
        or (
            "create an implementation plan for developer and reviewer handoff." in lowered
        )
    )


class LlmSingleDeveloperAgent:
    def __init__(self, executor: CodexExecutor) -> None:
        self._executor = executor

    async def develop(
        self,
        *,
        user_input: str,
        session: BotSession,
        round_index: int,
        review_feedback: str | None,
    ) -> str:
        del round_index
        del review_feedback

        selected_model = _select_agent_override(session.profile_agent_models, _DEVELOPER_AGENT_KEYS)
        if selected_model is None:
            selected_model = session.profile_model
        system_instructions = _select_agent_override(
            session.profile_agent_system_prompts,
            _DEVELOPER_AGENT_KEYS,
        )
        if system_instructions is None:
            system_instructions = _DEFAULT_DEVELOPER_SYSTEM_INSTRUCTIONS

        with codex_agent_name_scope("single.developer"):
            output = (
                await self._executor.run(
                    prompt=user_input,
                    history=session.history,
                    system_instructions=system_instructions,
                    model=selected_model,
                    cwd=session.profile_working_directory,
                )
            ).strip()

        if _looks_like_prompt_echo(output):
            raise CodexExecutionError(
                "executor returned prompt-like developer output; check executor configuration"
            )
        return output


@dataclass
class SingleAgentWorkflow:
    developer: DeveloperAgent

    async def run(self, input_text: str, session: BotSession) -> WorkflowResult:
        session.history = self._sanitize_history(session.history)
        candidate_output = await self.developer.develop(
            user_input=input_text,
            session=session,
            round_index=1,
            review_feedback=None,
        )

        next_history = [
            *session.history,
            {"role": "user", "content": input_text},
            {"role": "assistant", "content": candidate_output},
        ]

        return WorkflowResult(
            output_text=candidate_output,
            next_history=next_history,
            review_round=0,
            review_result="approved",
            metadata={
                "stage_transitions": [
                    {"from": "start", "to": "developer", "round": 1, "status": "completed"},
                    {"from": "developer", "to": "completed", "round": 1, "status": "approved"},
                ]
            },
        )

    @staticmethod
    def _sanitize_history(history: list[dict[str, Any]]) -> list[dict[str, Any]]:
        cleaned: list[dict[str, Any]] = []
        for item in history:
            if not isinstance(item, dict):
                continue
            role = str(item.get("role", "")).strip()
            if role not in {"user", "assistant"}:
                continue
            content = str(item.get("content", "")).strip()
            if not content:
                continue
            if _looks_like_prompt_echo(content):
                continue
            cleaned.append({"role": role, "content": content})
        if len(cleaned) > _MAX_HISTORY_ITEMS:
            cleaned = cleaned[-_MAX_HISTORY_ITEMS:]
        return cleaned


def _select_agent_override(
    mapping: dict[str, str],
    keys: tuple[str, ...],
) -> str | None:
    for key in keys:
        value = mapping.get(key)
        if isinstance(value, str):
            cleaned = value.strip()
            if cleaned:
                return cleaned
    return None
