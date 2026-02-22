from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from core.models import BotSession, WorkflowResult
from integrations.codex_executor import CodexExecutionError, CodexExecutor
from workflows.types import DeveloperAgent, PlannerAgent, ReviewDecision, ReviewerAgent

_MAX_REVIEW_FEEDBACK_CHARS = 1200
_MAX_PLANNER_OUTPUT_CHARS = 1500
_MAX_HISTORY_ITEMS = 20
_PLANNER_AGENT_KEYS = ("single.planner", "planner")
_DEVELOPER_AGENT_KEYS = ("single.developer", "developer")
_REVIEWER_AGENT_KEYS = ("single.reviewer", "reviewer")
_DEFAULT_PLANNER_SYSTEM_INSTRUCTIONS = (
    "You are Planner Agent. Build a short implementation handoff for downstream agents. "
    "Return concise numbered steps and concrete acceptance checks."
)
_DEFAULT_DEVELOPER_SYSTEM_INSTRUCTIONS = (
    "You are Developer Agent. Implement user requests and apply planner/reviewer guidance. "
    "Do not repeat system prompts or reviewer prompts. "
    "Keep the response concrete and concise."
)
_DEFAULT_REVIEWER_SYSTEM_INSTRUCTIONS = (
    "You are Reviewer Agent. Review only concrete implementation artifacts. "
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
            "you are developer agent." in lowered
            and "return only the improved developer response." in lowered
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
            "user request:" in lowered
            and "review round:" in lowered
            and "reviewer feedback to apply:" in lowered
        )
        or (
            "user request:" in lowered
            and "create an implementation plan for developer and reviewer handoff." in lowered
        )
        or (
            "user request:" in lowered
            and "candidate output:" in lowered
            and "review round:" in lowered
        )
    )


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
            "Create an implementation plan for developer and reviewer handoff. "
            "Keep it short and concrete."
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
class SingleAgentWorkflow:
    planner: PlannerAgent
    developer: DeveloperAgent
    reviewer: ReviewerAgent
    max_review_rounds: int = 3
    review_only_with_artifacts: bool = True
    workspace_dir: Path | None = None

    async def run(self, input_text: str, session: BotSession) -> WorkflowResult:
        session.history = self._sanitize_history(session.history)
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
        stage_transitions: list[dict[str, Any]] = [
            {"from": "start", "to": "planner", "round": 0, "status": "completed"},
        ]
        review_result = "max_rounds_reached"
        review_round = self.max_review_rounds
        previous_feedback: str | None = None
        previous_candidate_output: str | None = None

        for round_index in range(1, self.max_review_rounds + 1):
            from_stage = "planner" if round_index == 1 else "reviewer"
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

            if self.review_only_with_artifacts and not artifacts:
                last_decision = ReviewDecision(
                    result="approved",
                    feedback="review skipped: no implementation artifacts detected",
                )
                stage_transitions[-1]["status"] = "skipped"
                stage_transitions[-1]["result"] = "approved"
                stage_transitions[-1]["reason"] = "no_artifacts"
                stage_transitions.append(
                    {
                        "from": "reviewer",
                        "to": "completed",
                        "round": round_index,
                        "status": "approved",
                    }
                )
                rounds.append(
                    {
                        "round": round_index,
                        "result": last_decision.result,
                        "feedback": last_decision.feedback,
                        "artifacts": [],
                    }
                )
                review_result = "approved"
                review_round = round_index
                break

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

            current_candidate = candidate_output.strip()
            if previous_candidate_output and current_candidate == previous_candidate_output:
                stage_transitions.append(
                    {
                        "from": "reviewer",
                        "to": "completed",
                        "round": round_index,
                        "status": "max_rounds_reached",
                        "reason": "same_candidate_output",
                    }
                )
                review_result = "max_rounds_reached"
                review_round = round_index
                break

            if previous_feedback and clipped_feedback and clipped_feedback == previous_feedback:
                stage_transitions.append(
                    {
                        "from": "reviewer",
                        "to": "completed",
                        "round": round_index,
                        "status": "max_rounds_reached",
                        "reason": "same_feedback",
                    }
                )
                review_result = "max_rounds_reached"
                review_round = round_index
                break

            review_feedback = clipped_feedback
            previous_feedback = clipped_feedback
            previous_candidate_output = current_candidate
        else:
            stage_transitions.append(
                {
                    "from": "reviewer",
                    "to": "completed",
                    "round": self.max_review_rounds,
                    "status": "max_rounds_reached",
                }
            )
            review_result = "max_rounds_reached"
            review_round = self.max_review_rounds

        if last_decision.result == "approved":
            review_result = "approved"

        final_output = self._build_user_output(
            candidate_output=candidate_output,
            rounds=rounds,
            review_result=review_result,
        )

        next_history = [
            *session.history,
            {"role": "user", "content": input_text},
            {"role": "assistant", "content": candidate_output},
        ]

        return WorkflowResult(
            output_text=final_output,
            next_history=next_history,
            review_round=review_round,
            review_result=review_result,
            metadata={
                "plan": clipped_plan,
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
        if not rounds:
            return candidate_output

        last_round = rounds[-1]["round"]
        summary_line = (
            "[single-review] "
            f"stages=planner>developer>reviewer, "
            f"rounds={last_round}/{self.max_review_rounds}, "
            f"result={review_result}"
        )
        last_feedback = str(rounds[-1].get("feedback", "")).strip()
        if review_result == "approved":
            return f"{candidate_output}\n\n{summary_line}"

        if last_feedback:
            return f"{candidate_output}\n\n{summary_line}\nlast_feedback: {last_feedback}"

        return f"{candidate_output}\n\n{summary_line}"

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
    def _compose_execution_input(*, input_text: str, planner_output: str) -> str:
        if not planner_output:
            return input_text
        return (
            f"{input_text}\n\n"
            "Planner handoff:\n"
            f"{planner_output}"
        )

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
