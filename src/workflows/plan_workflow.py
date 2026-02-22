from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from core.models import BotSession, WorkflowResult
from integrations.codex_executor import CodexExecutionError, CodexExecutor, codex_agent_name_scope
from workflows.types import DeveloperAgent, PlannerAgent, ReviewDecision, ReviewerAgent, Workflow

_MAX_REVIEW_FEEDBACK_CHARS = 1200
_MAX_PLANNER_OUTPUT_CHARS = 1500
_MAX_HISTORY_ITEMS = 20
_PLANNER_AGENT_KEYS = ("plan.planner", "single.planner", "planner")
_DEVELOPER_AGENT_KEYS = ("plan.developer", "single.developer", "developer")
_REVIEWER_AGENT_KEYS = ("plan.reviewer", "single.reviewer", "reviewer")
_DEFAULT_PLANNER_SYSTEM_INSTRUCTIONS = (
    "You are Plan Planner Agent. Decide which stages are required and provide concise handoff text. "
    "Return strict JSON only."
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


@dataclass(frozen=True)
class PlannerGateDecision:
    use_single_agent: bool
    need_planner_handoff: bool
    need_developer: bool
    need_reviewer: bool
    reason: str
    execution_handoff: str


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
            "user request:" in lowered
            and "review round:" in lowered
            and "reviewer feedback to apply:" in lowered
        )
        or (
            "return strict json object with keys" in lowered
            and "need_planner_handoff" in lowered
            and "need_developer" in lowered
            and "need_reviewer" in lowered
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
            "Decide which plan stages are required. Return strict JSON object with keys: "
            "use_single_agent (boolean), need_planner_handoff (boolean), "
            "need_developer (boolean), need_reviewer (boolean), reason (string), "
            "execution_handoff (string).\n"
            "Rules:\n"
            "- If request is simple and plan/review stages are unnecessary, set use_single_agent=true.\n"
            "- If implementation/code change is not needed, set need_developer=false and need_reviewer=false.\n"
            "- If developer is required and review is useful, set need_reviewer=true.\n"
            "- execution_handoff must be concise actionable steps when need_planner_handoff=true.\n"
            "Return JSON only."
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
    planner: PlannerAgent
    developer: DeveloperAgent
    reviewer: ReviewerAgent
    single_fallback_workflow: Workflow | None = None
    max_review_rounds: int = 3
    review_only_with_artifacts: bool = True
    workspace_dir: Path | None = None

    async def run(self, input_text: str, session: BotSession) -> WorkflowResult:
        session.history = self._sanitize_history(session.history)
        planner_output = await self.planner.plan(user_input=input_text, session=session)
        gate = self._parse_planner_gate(planner_output)

        if gate.use_single_agent and self.single_fallback_workflow is not None:
            fallback_result = await self.single_fallback_workflow.run(input_text=input_text, session=session)
            fallback_metadata = fallback_result.get("metadata")
            if not isinstance(fallback_metadata, dict):
                fallback_metadata = {}
            fallback_metadata["planner_output"] = planner_output
            fallback_metadata["planner_gate"] = {
                "use_single_agent": gate.use_single_agent,
                "need_planner_handoff": gate.need_planner_handoff,
                "need_developer": gate.need_developer,
                "need_reviewer": gate.need_reviewer,
                "reason": gate.reason,
            }
            fallback_metadata["delegated_to"] = "single.developer"
            return WorkflowResult(
                output_text=fallback_result.get("output_text", ""),
                next_history=fallback_result.get("next_history", session.history),
                review_round=int(fallback_result.get("review_round", 0) or 0),
                review_result=fallback_result.get("review_result", "approved"),
                metadata=fallback_metadata,
            )

        clipped_plan = self._clip_planner_output(gate.execution_handoff)
        execution_input = self._compose_execution_input(
            input_text=input_text,
            planner_output=clipped_plan,
            include_planner_handoff=gate.need_planner_handoff,
        )

        review_feedback: str | None = None
        candidate_output = ""
        last_decision = ReviewDecision(result="approved", feedback="")
        rounds: list[dict[str, Any]] = []
        stage_transitions: list[dict[str, Any]] = [
            {"from": "start", "to": "planner", "round": 0, "status": "completed"},
        ]
        review_result = "approved"
        review_round = 0
        previous_feedback: str | None = None
        previous_candidate_output: str | None = None

        if not gate.need_developer:
            stage_transitions.append(
                {
                    "from": "planner",
                    "to": "completed",
                    "round": 0,
                    "status": "approved",
                    "reason": "planner_gate_no_developer",
                }
            )
            candidate_output = (
                clipped_plan
                or gate.reason
                or "planner gate: no developer stage required for this request"
            )
            rounds.append(
                {
                    "round": 0,
                    "result": "approved",
                    "feedback": "review skipped: developer stage not required",
                    "artifacts": [],
                }
            )
        else:
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
                    if gate.need_reviewer and self.review_only_with_artifacts
                    else {}
                )
                candidate_output = await self.developer.develop(
                    user_input=execution_input,
                    session=session,
                    round_index=round_index,
                    review_feedback=review_feedback,
                )

                if not gate.need_reviewer:
                    last_decision = ReviewDecision(
                        result="approved",
                        feedback="review skipped: planner gate disabled reviewer",
                    )
                    rounds.append(
                        {
                            "round": round_index,
                            "result": last_decision.result,
                            "feedback": last_decision.feedback,
                            "artifacts": [],
                        }
                    )
                    stage_transitions.append(
                        {
                            "from": "developer",
                            "to": "reviewer",
                            "round": round_index,
                            "status": "skipped",
                            "reason": "planner_gate_no_reviewer",
                        }
                    )
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

        if last_decision.result == "approved" and review_result != "max_rounds_reached":
            review_result = "approved"

        base_output = candidate_output.strip()
        if not base_output:
            base_output = clipped_plan or gate.reason

        final_output = self._build_user_output(
            candidate_output=base_output,
            rounds=rounds,
            review_result=review_result,
        )

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
                "planner_gate": {
                    "use_single_agent": gate.use_single_agent,
                    "need_planner_handoff": gate.need_planner_handoff,
                    "need_developer": gate.need_developer,
                    "need_reviewer": gate.need_reviewer,
                    "reason": gate.reason,
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
            "[plan-review] "
            "stages=planner>developer>reviewer, "
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
        include_planner_handoff: bool,
    ) -> str:
        if not include_planner_handoff or not planner_output:
            return input_text
        return f"{input_text}\n\nPlanner handoff:\n{planner_output}"

    @staticmethod
    def _parse_planner_gate(raw: str) -> PlannerGateDecision:
        payload = _extract_json_object(raw)
        if not isinstance(payload, dict):
            return PlannerGateDecision(
                use_single_agent=False,
                need_planner_handoff=True,
                need_developer=True,
                need_reviewer=True,
                reason="planner_gate_parse_failed",
                execution_handoff=raw.strip(),
            )

        use_single_agent = _bool_or_default(payload.get("use_single_agent"), False)
        need_planner_handoff = _bool_or_default(payload.get("need_planner_handoff"), True)
        need_developer = _bool_or_default(payload.get("need_developer"), True)
        need_reviewer = _bool_or_default(payload.get("need_reviewer"), True)
        reason = _string_or_default(payload.get("reason"), "")
        execution_handoff = _string_or_default(payload.get("execution_handoff"), "")
        if not execution_handoff:
            execution_handoff = _string_or_default(payload.get("plan"), "")

        if not need_developer:
            need_reviewer = False
            use_single_agent = True
        if use_single_agent:
            need_reviewer = False
        if not need_planner_handoff:
            execution_handoff = ""

        return PlannerGateDecision(
            use_single_agent=use_single_agent,
            need_planner_handoff=need_planner_handoff,
            need_developer=need_developer,
            need_reviewer=need_reviewer,
            reason=reason,
            execution_handoff=execution_handoff,
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


def _string_or_default(value: Any, default: str) -> str:
    if isinstance(value, str):
        cleaned = value.strip()
        if cleaned:
            return cleaned
    return default


def _bool_or_default(value: Any, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        cleaned = value.strip().lower()
        if cleaned in {"true", "1", "yes", "y", "on"}:
            return True
        if cleaned in {"false", "0", "no", "n", "off"}:
            return False
    return default


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
