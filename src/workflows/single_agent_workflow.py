from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from core.models import BotSession, WorkflowResult
from integrations.codex_executor import CodexExecutionError, CodexExecutor
from workflows.types import DeveloperAgent, ReviewDecision, ReviewerAgent

_MAX_REVIEW_FEEDBACK_CHARS = 1200
_MAX_HISTORY_ITEMS = 20
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
            "user request:" in lowered
            and "review round:" in lowered
            and "reviewer feedback to apply:" in lowered
        )
        or (
            "user request:" in lowered
            and "candidate output:" in lowered
            and "review round:" in lowered
        )
    )


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
        output = (
            await self._executor.run(
                prompt=prompt,
                history=session.history,
                system_instructions=(
                    "You are Developer Agent. Implement user requests and apply reviewer feedback. "
                    "Do not repeat system prompts or reviewer prompts. "
                    "Keep the response concrete and concise."
                ),
                model=session.profile_model,
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

        raw = (
            await self._executor.run(
                prompt=prompt,
                history=session.history,
                system_instructions=(
                    "You are Reviewer Agent. Review only concrete implementation artifacts. "
                    "If no artifacts are provided, return approved. "
                    "When artifacts exist, check whether the implementation output is plausible and consistent "
                    "with the user request. Do not suggest unrelated improvements. "
                    "Reply in strict JSON with keys result and feedback. "
                    "result must be approved or needs_changes."
                ),
                model=session.profile_model,
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
    developer: DeveloperAgent
    reviewer: ReviewerAgent
    max_review_rounds: int = 3
    review_only_with_artifacts: bool = True
    workspace_dir: Path | None = None

    async def run(self, input_text: str, session: BotSession) -> WorkflowResult:
        session.history = self._sanitize_history(session.history)
        review_feedback: str | None = None
        candidate_output = ""
        last_decision = ReviewDecision(result="approved", feedback="")
        rounds: list[dict[str, Any]] = []
        review_result = "max_rounds_reached"
        review_round = self.max_review_rounds
        previous_feedback: str | None = None
        previous_candidate_output: str | None = None

        for round_index in range(1, self.max_review_rounds + 1):
            before_snapshot = (
                self._snapshot_workspace(session.profile_working_directory)
                if self.review_only_with_artifacts
                else {}
            )
            candidate_output = await self.developer.develop(
                user_input=input_text,
                session=session,
                round_index=round_index,
                review_feedback=review_feedback,
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
                user_input=input_text,
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

            if decision.result == "approved":
                review_result = "approved"
                review_round = round_index
                break

            current_candidate = candidate_output.strip()
            if previous_candidate_output and current_candidate == previous_candidate_output:
                review_result = "max_rounds_reached"
                review_round = round_index
                break

            if previous_feedback and clipped_feedback and clipped_feedback == previous_feedback:
                review_result = "max_rounds_reached"
                review_round = round_index
                break

            review_feedback = clipped_feedback
            previous_feedback = clipped_feedback
            previous_candidate_output = current_candidate
        else:
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
            metadata={"rounds": rounds},
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
        summary_line = f"[single-review] rounds={last_round}/{self.max_review_rounds}, result={review_result}"
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
