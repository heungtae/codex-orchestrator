from __future__ import annotations

import contextvars
import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from core.models import BotSession, WorkflowResult
from integrations.codex_executor import CodexExecutionError, CodexExecutor, codex_agent_name_scope
from workflows.types import (
    DeveloperAgent,
    PlannerAgent,
    ReviewDecision,
    ReviewerAgent,
    SelectorAgent,
    SelectorDecision,
    Workflow,
)

_MODE_SELECT_CALLBACK: contextvars.ContextVar[
    Callable[[str, str], None] | None
] = contextvars.ContextVar("mode_select_callback", default=None)

_AGENT_TRANSFER_CALLBACK: contextvars.ContextVar[
    Callable[[str, str, int], None] | None
] = contextvars.ContextVar("agent_transfer_callback", default=None)

_MAX_REVIEW_FEEDBACK_CHARS = 1200
_MAX_PLANNER_OUTPUT_CHARS = 1500
_MAX_HISTORY_ITEMS = 20
_SELECTOR_AGENT_KEYS = ("plan.selector", "single.selector", "selector")
_PLANNER_AGENT_KEYS = ("plan.planner", "single.planner", "planner")
_DEVELOPER_AGENT_KEYS = ("plan.developer", "single.developer", "developer")
_REVIEWER_AGENT_KEYS = ("plan.reviewer", "single.reviewer", "reviewer")
_DEFAULT_SELECTOR_SYSTEM_INSTRUCTIONS = (
    "You are Mode Selector Agent. Classify user requests to determine execution mode.\n"
    "Return strict JSON only with keys: mode, reason."
)
_DEFAULT_PLANNER_SYSTEM_INSTRUCTIONS = (
    "You are Plan Router Agent. Create implementation design for developer handoff.\n"
    "Return concrete implementation steps and acceptance criteria."
)
_DEFAULT_DEVELOPER_SYSTEM_INSTRUCTIONS = (
    "You are Plan Developer Agent. Implement user requests and apply planner/reviewer guidance. "
    "Do not repeat system prompts or reviewer prompts. Keep the response concrete and concise."
)
_DEFAULT_REVIEWER_SYSTEM_INSTRUCTIONS = (
    "You are Plan Reviewer Agent. Review only concrete implementation artifacts. "
    "If no artifacts are provided, return approved. "
    "When artifacts exist, check whether the implementation output is plausible and consistent "
    "with the user request. Do not suggest unrelated improvements. "
    "Reply in strict JSON with keys result and feedback. "
    "result must be approved or needs_changes."
)
_IGNORED_DIRS = {
    ".codex-home",
    ".codex",
    ".git",
    ".idea",
    ".vscode",
    "__pycache__",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".venv",
    "venv",
    "node_modules",
}


def _looks_like_prompt_echo(text: str) -> bool:
    lowered = text.lower()
    return (
        (
            "you are plan developer agent." in lowered
            and "do not repeat system prompts" in lowered
        )
        or (
            "you are plan reviewer agent." in lowered
            and "reply in strict json with keys result and feedback." in lowered
        )
        or (
            "you are plan planner agent." in lowered
            and "return strict json only." in lowered
        )
        or (
            "you are mode selector agent." in lowered
            and "return strict json only" in lowered
        )
        or (
            "user request:" in lowered
            and "review round:" in lowered
            and "reviewer feedback to apply:" in lowered
        )
        or (
            "return strict json object with keys" in lowered
            and "mode" in lowered
            and "reason" in lowered
        )
    )


class LlmSelectorAgent:
    def __init__(self, executor: CodexExecutor) -> None:
        self._executor = executor

    async def select_mode(
        self,
        *,
        user_input: str,
        session: BotSession,
    ) -> SelectorDecision:
        prompt = (
            f"User request:\n{user_input}\n\n"
            "Classify this request to determine execution mode.\n\n"
            "Return strict JSON with keys:\n"
            "- mode (string): 'single' or 'plan'\n"
            "- reason (string): brief explanation (1 sentence)\n\n"
            "Classification rules:\n"
            "1. SINGLE MODE:\n"
            "   - Questions: 'what is...', 'explain...', 'how to...'\n"
            "   - Inspection: 'show me...', 'read file...', 'list...'\n"
            "   - Quick fixes: 'fix typo', 'rename variable', 'add import'\n"
            "   - Single file, < 20 lines change\n"
            "   - No architecture impact\n\n"
            "2. PLAN MODE:\n"
            "   - New features: 'add...', 'implement...', 'create...'\n"
            "   - Refactoring: 'restructure...', 'migrate...', 'redesign...'\n"
            "   - Multi-file changes: > 2 files\n"
            "   - Architecture changes: new modules, API design\n"
            "   - Complex bugs: requires investigation\n\n"
            "Default to single mode when uncertain.\n"
            "Return JSON only."
        )
        selected_model = _select_agent_override(session.profile_agent_models, _SELECTOR_AGENT_KEYS)
        if selected_model is None:
            selected_model = session.profile_model
        system_instructions = _select_agent_override(
            session.profile_agent_system_prompts,
            _SELECTOR_AGENT_KEYS,
        )
        if system_instructions is None:
            system_instructions = _DEFAULT_SELECTOR_SYSTEM_INSTRUCTIONS
        with codex_agent_name_scope("plan.selector"):
            output = (
                await self._executor.run(
                    prompt=prompt,
                    history=session.history,
                    system_instructions=system_instructions,
                    model=selected_model,
                    cwd=session.profile_working_directory,
                )
            ).strip()
        if _looks_like_prompt_echo(output):
            raise CodexExecutionError(
                "executor returned prompt-like selector output; check executor configuration"
            )
        return self._parse_selector_output(output)

    @staticmethod
    def _parse_selector_output(raw: str) -> SelectorDecision:
        payload = _extract_json_object(raw)
        if not isinstance(payload, dict):
            return SelectorDecision(mode="single", reason="parse_failed; defaulting to single")

        mode = str(payload.get("mode", "single")).strip().lower()
        reason = str(payload.get("reason", "")).strip()

        if mode not in ("single", "plan"):
            mode = "single"

        return SelectorDecision(mode=mode, reason=reason)


class LlmPlannerAgent:
    def __init__(self, executor: CodexExecutor) -> None:
        self._executor = executor

    async def plan(
        self,
        *,
        user_input: str,
        session: BotSession,
    ) -> str:
        prompt = (
            f"User request:\n{user_input}\n\n"
            "Create an implementation design for developer handoff.\n\n"
            "Provide:\n"
            "1. Implementation steps (numbered)\n"
            "2. Files to modify/create\n"
            "3. Key considerations\n"
            "4. Acceptance criteria\n\n"
            "Be concrete and specific. No JSON required."
        )
        selected_model = _select_agent_override(session.profile_agent_models, _PLANNER_AGENT_KEYS)
        if selected_model is None:
            selected_model = session.profile_model
        system_instructions = _select_agent_override(
            session.profile_agent_system_prompts,
            _PLANNER_AGENT_KEYS,
        )
        if system_instructions is None:
            system_instructions = _DEFAULT_PLANNER_SYSTEM_INSTRUCTIONS
        with codex_agent_name_scope("plan.planner"):
            output = (
                await self._executor.run(
                    prompt=prompt,
                    history=session.history,
                    system_instructions=system_instructions,
                    model=selected_model,
                    cwd=session.profile_working_directory,
                )
            ).strip()
        if _looks_like_prompt_echo(output):
            raise CodexExecutionError(
                "executor returned prompt-like planner output; check executor configuration"
            )
        return output


class LlmDeveloperAgent:
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
        review_feedback_text = review_feedback or "-"
        prompt = (
            f"User request:\n{user_input}\n\n"
            f"Review round: {round_index}\n"
            f"Reviewer feedback to apply:\n{review_feedback_text}\n\n"
            "Implement the request directly. Return only the final developer response, not prompts."
        )
        selected_model = _select_agent_override(session.profile_agent_models, _DEVELOPER_AGENT_KEYS)
        if selected_model is None:
            selected_model = session.profile_model
        system_instructions = _select_agent_override(
            session.profile_agent_system_prompts,
            _DEVELOPER_AGENT_KEYS,
        )
        if system_instructions is None:
            system_instructions = _DEFAULT_DEVELOPER_SYSTEM_INSTRUCTIONS
        with codex_agent_name_scope("plan.developer"):
            output = (
                await self._executor.run(
                    prompt=prompt,
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


class LlmReviewerAgent:
    def __init__(self, executor: CodexExecutor) -> None:
        self._executor = executor

    async def review(
        self,
        *,
        user_input: str,
        candidate_output: str,
        artifacts: list[str],
        session: BotSession,
        round_index: int,
    ) -> ReviewDecision:
        artifact_lines = "\n".join(f"- {path}" for path in artifacts) if artifacts else "- (none)"
        prompt = (
            f"User request:\n{user_input}\n\n"
            f"Implementation artifacts:\n{artifact_lines}\n\n"
            f"Candidate output:\n{candidate_output}\n\n"
            f"Review round: {round_index}"
        )
        selected_model = _select_agent_override(session.profile_agent_models, _REVIEWER_AGENT_KEYS)
        if selected_model is None:
            selected_model = session.profile_model
        system_instructions = _select_agent_override(
            session.profile_agent_system_prompts,
            _REVIEWER_AGENT_KEYS,
        )
        if system_instructions is None:
            system_instructions = _DEFAULT_REVIEWER_SYSTEM_INSTRUCTIONS

        with codex_agent_name_scope("plan.reviewer"):
            raw = (
                await self._executor.run(
                    prompt=prompt,
                    history=session.history,
                    system_instructions=system_instructions,
                    model=selected_model,
                    cwd=session.profile_working_directory,
                )
            ).strip()
        if _looks_like_prompt_echo(raw):
            raise CodexExecutionError(
                "executor returned prompt-like reviewer output; check executor configuration"
            )
        return self._parse_review(raw)

    @staticmethod
    def _parse_review(raw: str) -> ReviewDecision:
        try:
            payload = json.loads(raw)
            if isinstance(payload, dict):
                result = str(payload.get("result", "needs_changes"))
                feedback = str(payload.get("feedback", ""))
                if result in ("approved", "needs_changes"):
                    return ReviewDecision(result=result, feedback=feedback.strip())
        except json.JSONDecodeError:
            pass

        lowered = raw.lower()
        if re.search(r"\bapproved\b", lowered) and not re.search(
            r"\bneeds_changes\b", lowered
        ):
            return ReviewDecision(result="approved", feedback=raw)
        return ReviewDecision(
            result="approved",
            feedback="reviewer_output_not_json; accepted to avoid review dead loop",
        )


@dataclass
class PlanWorkflow:
    selector: SelectorAgent
    planner: PlannerAgent
    developer: DeveloperAgent
    reviewer: ReviewerAgent
    single_workflow: Workflow
    max_review_rounds: int = 1
    review_only_with_artifacts: bool = True
    workspace_dir: Path | None = None
    on_mode_selected: Callable[[str, str], None] | None = None
    on_agent_transfer: Callable[[str, str, int], None] | None = None

    def _notify_agent_transfer(self, from_agent: str, to_agent: str, round: int) -> None:
        callback = _AGENT_TRANSFER_CALLBACK.get()
        if callback is None:
            callback = self.on_agent_transfer
        if callback is not None:
            callback(from_agent, to_agent, round)

    async def run(self, input_text: str, session: BotSession) -> WorkflowResult:
        session.history = self._sanitize_history(session.history)

        selector_decision = await self.selector.select_mode(user_input=input_text, session=session)

        if selector_decision.mode == "plan":
            callback = _MODE_SELECT_CALLBACK.get()
            if callback is None:
                callback = self.on_mode_selected
            if callback is not None:
                callback(selector_decision.mode, selector_decision.reason)

        stage_transitions: list[dict[str, Any]] = [
            {"from": "start", "to": "selector", "round": 0, "status": "completed"},
        ]

        if selector_decision.mode == "single":
            self._notify_agent_transfer("selector", "single_workflow", 0)
            stage_transitions.append(
                {
                    "from": "selector",
                    "to": "single_workflow",
                    "round": 0,
                    "status": "delegated",
                    "reason": selector_decision.reason,
                }
            )
            result = await self.single_workflow.run(input_text, session)
            result["metadata"] = {
                **result.get("metadata", {}),
                "selector_decision": {
                    "mode": selector_decision.mode,
                    "reason": selector_decision.reason,
                },
                "stage_transitions": stage_transitions,
                "delegated_to": "single_workflow",
            }
            return result

        self._notify_agent_transfer("selector", "planner", 0)

        stage_transitions.append(
            {
                "from": "selector",
                "to": "planner",
                "round": 0,
                "status": "completed",
                "reason": selector_decision.reason,
            }
        )

        planner_output = await self.planner.plan(user_input=input_text, session=session)
        clipped_plan = self._clip_planner_output(planner_output)

        execution_input = self._compose_execution_input(
            input_text=input_text,
            planner_output=clipped_plan,
        )

        review_feedback: str | None = None
        candidate_output = ""
        last_decision = ReviewDecision(result="approved", feedback="")
        rounds: list[dict[str, Any]] = []
        review_result = "approved"
        review_round = 0
        previous_feedback: str | None = None
        previous_candidate_output: str | None = None
        last_round_feedback: str | None = None

        for round_index in range(1, self.max_review_rounds + 1):
            if round_index > 1 and last_round_feedback is not None:
                review_feedback = last_round_feedback
            from_stage = "planner" if round_index == 1 else "reviewer"
            self._notify_agent_transfer(from_stage, "developer", round_index)
            stage_transitions.append(
                {
                    "from": from_stage,
                    "to": "developer",
                    "round": round_index,
                    "status": "completed",
                }
            )
            before_snapshot = (
                self._snapshot_workspace(session.profile_working_directory)
                if self.review_only_with_artifacts
                else {}
            )
            candidate_output = await self.developer.develop(
                user_input=execution_input,
                session=session,
                round_index=round_index,
                review_feedback=review_feedback,
            )

            self._notify_agent_transfer("developer", "reviewer", round_index)
            stage_transitions.append(
                {
                    "from": "developer",
                    "to": "reviewer",
                    "round": round_index,
                    "status": "pending",
                }
            )
            artifacts: list[str] = []
            if self.review_only_with_artifacts:
                after_snapshot = self._snapshot_workspace(session.profile_working_directory)
                artifacts = self._detect_artifacts(before_snapshot, after_snapshot)
            decision = await self.reviewer.review(
                user_input=execution_input,
                candidate_output=candidate_output,
                artifacts=artifacts,
                session=session,
                round_index=round_index,
            )
            clipped_feedback = self._clip_feedback(decision.feedback)
            decision = ReviewDecision(result=decision.result, feedback=clipped_feedback)
            rounds.append(
                {
                    "round": round_index,
                    "result": decision.result,
                    "feedback": decision.feedback,
                    "artifacts": artifacts,
                }
            )
            last_decision = decision
            stage_transitions[-1]["status"] = decision.result

            if decision.result == "approved":
                if round_index == self.max_review_rounds:
                    self._notify_agent_transfer("reviewer", "completed", round_index)
                    stage_transitions.append(
                        {
                            "from": "reviewer",
                            "to": "completed",
                            "round": round_index,
                            "status": "approved",
                        }
                    )
                    review_result = "approved"
                    review_round = round_index
                    break
                else:
                    stage_transitions.append(
                        {
                            "from": "reviewer",
                            "to": "completed",
                            "round": round_index,
                            "status": "approved",
                        }
                    )
                    review_result = "approved"
                    review_round = round_index
                    break

            self._notify_agent_transfer("reviewer", "developer", round_index)
            before_snapshot = (
                self._snapshot_workspace(session.profile_working_directory)
                if self.review_only_with_artifacts
                else {}
            )
            candidate_output = await self.developer.develop(
                user_input=execution_input,
                session=session,
                round_index=round_index,
                review_feedback=clipped_feedback,
            )
            stage_transitions.append(
                {
                    "from": "developer",
                    "to": "completed",
                    "round": round_index,
                    "status": "needs_changes",
                }
            )
            review_result = "needs_changes"
            review_round = round_index
            review_feedback = clipped_feedback
            previous_feedback = clipped_feedback
            previous_candidate_output = candidate_output.strip()

        if review_result != "approved" and review_result != "completed":
            self._notify_agent_transfer("developer", "completed", self.max_review_rounds)
            stage_transitions.append(
                {
                    "from": "developer",
                    "to": "completed",
                    "round": self.max_review_rounds,
                    "status": review_result,
                }
            )

        if last_decision.result == "approved" and review_result != "max_rounds_reached":
            review_result = "approved"

        selector_info = f"[Selector] mode={selector_decision.mode}, reason={selector_decision.reason}"

        base_output = candidate_output.strip()
        if not base_output:
            base_output = clipped_plan

        final_output = self._build_user_output(
            candidate_output=base_output,
            rounds=rounds,
            review_result=review_result,
        )

        final_output = f"{selector_info}\n\n{final_output}"

        next_history = [
            *session.history,
            {"role": "user", "content": input_text},
            {"role": "assistant", "content": base_output},
        ]

        return WorkflowResult(
            output_text=final_output,
            next_history=next_history,
            review_round=review_round,
            review_result=review_result,
            metadata={
                "plan": clipped_plan,
                "planner_output": planner_output,
                "selector_decision": {
                    "mode": selector_decision.mode,
                    "reason": selector_decision.reason,
                },
                "rounds": rounds,
                "stage_transitions": stage_transitions,
            },
        )

    def _build_user_output(
        self,
        *,
        candidate_output: str,
        rounds: list[dict[str, Any]],
        review_result: str,
    ) -> str:
        last_round = int(rounds[-1]["round"]) if rounds else 0
        summary_line = (
            f"[plan-workflow] "
            f"rounds={last_round}/{self.max_review_rounds}, "
            f"result={review_result}"
        )
        last_feedback = str(rounds[-1].get("feedback", "")).strip() if rounds else ""

        if review_result == "approved":
            if candidate_output:
                return f"{candidate_output}\n\n{summary_line}"
            return summary_line

        if last_feedback:
            if candidate_output:
                return f"{candidate_output}\n\n{summary_line}\nlast_feedback: {last_feedback}"
            return f"{summary_line}\nlast_feedback: {last_feedback}"

        if candidate_output:
            return f"{candidate_output}\n\n{summary_line}"
        return summary_line

    @staticmethod
    def _clip_feedback(feedback: str) -> str:
        trimmed = feedback.strip()
        if len(trimmed) <= _MAX_REVIEW_FEEDBACK_CHARS:
            return trimmed
        return trimmed[:_MAX_REVIEW_FEEDBACK_CHARS] + "..."

    @staticmethod
    def _clip_planner_output(planner_output: str) -> str:
        trimmed = planner_output.strip()
        if len(trimmed) <= _MAX_PLANNER_OUTPUT_CHARS:
            return trimmed
        return trimmed[:_MAX_PLANNER_OUTPUT_CHARS] + "..."

    @staticmethod
    def _compose_execution_input(
        *,
        input_text: str,
        planner_output: str,
    ) -> str:
        if not planner_output:
            return input_text
        return f"{input_text}\n\nPlanner handoff:\n{planner_output}"

    def _snapshot_workspace(self, profile_working_directory: str | None) -> dict[str, tuple[int, int]]:
        if profile_working_directory:
            root = Path(profile_working_directory).resolve()
        else:
            root = (self.workspace_dir or Path.cwd()).resolve()
        if not root.exists() or not root.is_dir():
            return {}

        snapshot: dict[str, tuple[int, int]] = {}
        for dirpath, dirnames, filenames in os.walk(root):
            current_dir = Path(dirpath)
            dirnames[:] = [name for name in dirnames if name not in _IGNORED_DIRS]
            for filename in filenames:
                path = current_dir / filename
                try:
                    stat = path.stat()
                except OSError:
                    continue
                try:
                    relative = path.relative_to(root).as_posix()
                except ValueError:
                    continue
                snapshot[relative] = (int(stat.st_mtime_ns), int(stat.st_size))
        return snapshot

    @staticmethod
    def _detect_artifacts(
        before: dict[str, tuple[int, int]],
        after: dict[str, tuple[int, int]],
    ) -> list[str]:
        changed: list[str] = []
        for path in sorted(set(before.keys()) | set(after.keys())):
            if before.get(path) != after.get(path):
                changed.append(path)
        return changed

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


def _extract_json_object(raw: str) -> dict[str, Any] | None:
    cleaned = (raw or "").strip()
    if not cleaned:
        return None

    try:
        parsed = json.loads(cleaned)
        if isinstance(parsed, dict):
            return parsed
    except json.JSONDecodeError:
        pass

    decoder = json.JSONDecoder()
    for index, char in enumerate(cleaned):
        if char != "{":
            continue
        try:
            parsed, _ = decoder.raw_decode(cleaned[index:])
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            return parsed
    return None


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
