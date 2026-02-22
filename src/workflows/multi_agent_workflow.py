from __future__ import annotations

from dataclasses import dataclass

from core.models import BotSession, WorkflowResult
from integrations.codex_executor import CodexExecutor

_MULTI_AGENT_KEYS = (
    "multi.manager",
    "multi.designer",
    "multi.frontend.developer",
    "multi.backend.developer",
    "multi.tester",
    "multi",
)


@dataclass
class MultiAgentWorkflow:
    """Placeholder multi-agent flow.

    Single mode is prioritized; this class keeps the contract stable
    until handoff-based multi-agent orchestration is implemented.
    """

    executor: CodexExecutor

    async def run(self, input_text: str, session: BotSession) -> WorkflowResult:
        selected_model = _select_agent_override(session.profile_agent_models)
        if selected_model is None:
            selected_model = session.profile_model
        system_instructions = _select_agent_override(session.profile_agent_system_prompts)
        output = await self.executor.run(
            prompt=input_text,
            history=session.history,
            system_instructions=system_instructions,
            model=selected_model,
            cwd=session.profile_working_directory,
        )
        next_history = [
            *session.history,
            {"role": "user", "content": input_text},
            {"role": "assistant", "content": output},
        ]
        return WorkflowResult(output_text=output, next_history=next_history)


def _select_agent_override(mapping: dict[str, str]) -> str | None:
    for key in _MULTI_AGENT_KEYS:
        value = mapping.get(key)
        if isinstance(value, str):
            cleaned = value.strip()
            if cleaned:
                return cleaned
    return None
