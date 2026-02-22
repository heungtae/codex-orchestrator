from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from core.models import BotSession, WorkflowResult
from integrations.codex_executor import CodexExecutor

_MAX_HISTORY_ITEMS = 20
_MAX_ROLE_OUTPUT_CHARS = 2500
_ROLE_ORDER = ("designer", "frontend_developer", "backend_developer", "tester")
_ROLE_LABELS = {
    "manager": "Project Manager",
    "designer": "Designer",
    "frontend_developer": "Frontend Developer",
    "backend_developer": "Backend Developer",
    "tester": "Tester",
}
_ROLE_AGENT_KEYS = {
    "manager": ("multi.manager", "multi"),
    "designer": ("multi.designer", "multi"),
    "frontend_developer": ("multi.frontend.developer", "multi.frontend", "multi"),
    "backend_developer": ("multi.backend.developer", "multi.backend", "multi"),
    "tester": ("multi.tester", "multi"),
}
_DEFAULT_ROLE_TASKS = {
    "designer": (
        "Prepare concise UI/UX and interaction notes relevant to the user request. "
        "If UI is not relevant, provide concise design notes for developer handoff."
    ),
    "frontend_developer": (
        "Implement frontend-facing changes and integrate with backend contracts where needed."
    ),
    "backend_developer": (
        "Implement backend-facing changes, API behavior, and server-side logic as needed."
    ),
    "tester": (
        "Verify the implementation with practical checks and call out clear pass/fail criteria."
    ),
}
_DEFAULT_ROLE_SYSTEM_INSTRUCTIONS = {
    "manager": (
        "You are the Project Manager. Produce concrete plans and final summaries. "
        "Do not output internal chain-of-thought."
    ),
    "designer": (
        "You are the Designer. Focus on practical design artifacts and actionable guidance."
    ),
    "frontend_developer": (
        "You are the Frontend Developer. Implement exactly what is requested and keep code readable."
    ),
    "backend_developer": (
        "You are the Backend Developer. Implement requested APIs/logic with minimal complexity."
    ),
    "tester": (
        "You are the Tester. Validate expected behavior and clearly report verification steps/results."
    ),
}
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
class RolePlan:
    enabled: bool
    task: str
    required_outputs: tuple[str, ...]


@dataclass(frozen=True)
class WorkflowPlan:
    project_name: str
    summary: str
    final_goal: str
    roles: dict[str, RolePlan]


@dataclass(frozen=True)
class RoleRunResult:
    role: str
    enabled: bool
    output: str
    artifacts: tuple[str, ...]
    missing_outputs: tuple[str, ...]


@dataclass
class MultiAgentWorkflow:
    executor: CodexExecutor
    max_role_retries: int = 1
    workspace_dir: Path | None = None

    async def run(self, input_text: str, session: BotSession) -> WorkflowResult:
        session.history = self._sanitize_history(session.history)

        plan, planning_output = await self._create_plan(input_text=input_text, session=session)
        stage_results: list[RoleRunResult] = []

        for role in _ROLE_ORDER:
            role_plan = plan.roles.get(role)
            if role_plan is None or not role_plan.enabled:
                stage_results.append(
                    RoleRunResult(
                        role=role,
                        enabled=False,
                        output="",
                        artifacts=(),
                        missing_outputs=(),
                    )
                )
                continue

            stage_results.append(
                await self._execute_role(
                    role=role,
                    role_plan=role_plan,
                    input_text=input_text,
                    plan=plan,
                    previous_stage_results=stage_results,
                    session=session,
                )
            )

        final_output = await self._finalize(
            input_text=input_text,
            plan=plan,
            planning_output=planning_output,
            stage_results=stage_results,
            session=session,
        )
        final_output = final_output.strip() or self._build_fallback_output(plan, stage_results)
        summary_line = self._build_summary_line(stage_results)
        merged_output = f"{final_output}\n\n{summary_line}"

        next_history = [
            *session.history,
            {"role": "user", "content": input_text},
            {"role": "assistant", "content": merged_output},
        ]
        metadata = {
            "project_name": plan.project_name,
            "stages": [
                {
                    "role": result.role,
                    "enabled": result.enabled,
                    "artifacts": list(result.artifacts),
                    "missing_outputs": list(result.missing_outputs),
                }
                for result in stage_results
            ],
        }
        return WorkflowResult(output_text=merged_output, next_history=next_history, metadata=metadata)

    async def _create_plan(self, *, input_text: str, session: BotSession) -> tuple[WorkflowPlan, str]:
        planning_prompt = self._build_plan_prompt(input_text)
        planning_output = await self._run_agent(
            role="manager",
            prompt=planning_prompt,
            session=session,
        )
        parsed_plan = self._parse_plan(planning_output)
        if parsed_plan is None:
            return self._default_plan(input_text), planning_output
        return parsed_plan, planning_output

    async def _execute_role(
        self,
        *,
        role: str,
        role_plan: RolePlan,
        input_text: str,
        plan: WorkflowPlan,
        previous_stage_results: list[RoleRunResult],
        session: BotSession,
    ) -> RoleRunResult:
        current_output = ""
        artifact_set: set[str] = set()
        missing_outputs: list[str] = []

        for attempt in range(1, self.max_role_retries + 2):
            before_snapshot = self._snapshot_workspace(session.profile_working_directory)
            role_prompt = self._build_role_prompt(
                role=role,
                role_plan=role_plan,
                input_text=input_text,
                plan=plan,
                previous_stage_results=previous_stage_results,
                attempt=attempt,
                missing_outputs=missing_outputs,
            )
            current_output = await self._run_agent(role=role, prompt=role_prompt, session=session)
            after_snapshot = self._snapshot_workspace(session.profile_working_directory)
            artifact_set.update(self._detect_artifacts(before_snapshot, after_snapshot))

            missing_outputs = self._find_missing_outputs(
                role_plan.required_outputs,
                session.profile_working_directory,
            )
            if not missing_outputs:
                break

        return RoleRunResult(
            role=role,
            enabled=True,
            output=current_output,
            artifacts=tuple(sorted(artifact_set)),
            missing_outputs=tuple(missing_outputs),
        )

    async def _finalize(
        self,
        *,
        input_text: str,
        plan: WorkflowPlan,
        planning_output: str,
        stage_results: list[RoleRunResult],
        session: BotSession,
    ) -> str:
        finalize_prompt = self._build_finalize_prompt(
            input_text=input_text,
            plan=plan,
            planning_output=planning_output,
            stage_results=stage_results,
        )
        return await self._run_agent(role="manager", prompt=finalize_prompt, session=session)

    async def _run_agent(self, *, role: str, prompt: str, session: BotSession) -> str:
        role_keys = _ROLE_AGENT_KEYS.get(role, ("multi",))
        selected_model = _select_agent_override(session.profile_agent_models, role_keys)
        if selected_model is None:
            selected_model = session.profile_model
        system_instructions = _select_agent_override(session.profile_agent_system_prompts, role_keys)
        if system_instructions is None:
            system_instructions = _DEFAULT_ROLE_SYSTEM_INSTRUCTIONS.get(role)
        output = await self.executor.run(
            prompt=prompt,
            history=session.history,
            system_instructions=system_instructions,
            model=selected_model,
            cwd=session.profile_working_directory,
        )
        return output.strip()

    def _build_plan_prompt(self, input_text: str) -> str:
        role_schema = ", ".join(f'"{role}"' for role in _ROLE_ORDER)
        return (
            "Create a multi-agent execution plan for the user request.\n"
            "Return strict JSON only. Do not use markdown.\n"
            "Schema:\n"
            "{\n"
            '  "project_name": "string",\n'
            '  "summary": "string",\n'
            '  "final_goal": "string",\n'
            '  "roles": {\n'
            '    "designer": {"enabled": true, "task": "string", "required_outputs": ["path"]},\n'
            '    "frontend_developer": {"enabled": true, "task": "string", "required_outputs": ["path"]},\n'
            '    "backend_developer": {"enabled": true, "task": "string", "required_outputs": ["path"]},\n'
            '    "tester": {"enabled": true, "task": "string", "required_outputs": ["path"]}\n'
            "  }\n"
            "}\n"
            f"Use only these role keys: {role_schema}.\n"
            "If a role is not needed, set enabled=false and keep required_outputs as an empty list.\n\n"
            f"User request:\n{input_text}"
        )

    def _build_role_prompt(
        self,
        *,
        role: str,
        role_plan: RolePlan,
        input_text: str,
        plan: WorkflowPlan,
        previous_stage_results: list[RoleRunResult],
        attempt: int,
        missing_outputs: list[str],
    ) -> str:
        required_outputs = (
            "\n".join(f"- {item}" for item in role_plan.required_outputs)
            if role_plan.required_outputs
            else "- (none)"
        )
        prior_outputs = self._render_prior_outputs(previous_stage_results)
        role_label = _ROLE_LABELS.get(role, role)
        lines = [
            f"Project name: {plan.project_name}",
            f"Project summary: {plan.summary}",
            f"Final goal: {plan.final_goal}",
            "",
            f"User request:\n{input_text}",
            "",
            f"Role: {role_label}",
            f"Role task:\n{role_plan.task}",
            "",
            f"Required outputs:\n{required_outputs}",
            "",
            f"Prior role outputs:\n{prior_outputs}",
            "",
            "Execute this role now. Create or update repository files as needed.",
            "Return a concise result summary and key files touched.",
        ]
        if attempt > 1 and missing_outputs:
            missing_text = "\n".join(f"- {item}" for item in missing_outputs)
            lines.extend(
                [
                    "",
                    "The following required outputs are still missing. Create them in this attempt:",
                    missing_text,
                ]
            )
        return "\n".join(lines)

    def _build_finalize_prompt(
        self,
        *,
        input_text: str,
        plan: WorkflowPlan,
        planning_output: str,
        stage_results: list[RoleRunResult],
    ) -> str:
        stages_text = self._render_stage_results(stage_results)
        return (
            f"User request:\n{input_text}\n\n"
            f"Planning output:\n{self._clip_text(planning_output)}\n\n"
            f"Project name: {plan.project_name}\n"
            f"Project summary: {plan.summary}\n"
            f"Final goal: {plan.final_goal}\n\n"
            f"Role execution results:\n{stages_text}\n\n"
            "Generate the final user-facing response with:\n"
            "1) completed work\n"
            "2) key files/artifacts\n"
            "3) remaining gaps (only if any)\n"
            "Keep it concise and implementation-focused."
        )

    def _default_plan(self, input_text: str) -> WorkflowPlan:
        project_name = input_text.strip().splitlines()[0][:80] or "multi-agent-task"
        roles = {
            role: RolePlan(
                enabled=True,
                task=_DEFAULT_ROLE_TASKS.get(role, "Implement role responsibilities."),
                required_outputs=(),
            )
            for role in _ROLE_ORDER
        }
        return WorkflowPlan(
            project_name=project_name,
            summary="Execute the request through designer, implementation, and verification roles.",
            final_goal="Complete requested changes with concise artifacts and verification.",
            roles=roles,
        )

    def _parse_plan(self, raw: str) -> WorkflowPlan | None:
        payload = _extract_json_dict(raw)
        if payload is None:
            return None

        project_name = _string_or_default(payload.get("project_name"), "multi-agent-task")
        summary = _string_or_default(
            payload.get("summary"),
            "Execute the request through designer, implementation, and verification roles.",
        )
        final_goal = _string_or_default(
            payload.get("final_goal"),
            "Complete requested changes with concise artifacts and verification.",
        )

        raw_roles = payload.get("roles")
        if not isinstance(raw_roles, dict):
            raw_roles = {}

        roles: dict[str, RolePlan] = {}
        for role in _ROLE_ORDER:
            raw_role = raw_roles.get(role)
            if not isinstance(raw_role, dict):
                raw_role = {}

            enabled = _bool_or_default(raw_role.get("enabled"), True)
            task = _string_or_default(raw_role.get("task"), _DEFAULT_ROLE_TASKS[role])
            required_outputs = tuple(self._normalize_outputs(raw_role.get("required_outputs")))
            roles[role] = RolePlan(
                enabled=enabled,
                task=task,
                required_outputs=required_outputs,
            )

        return WorkflowPlan(
            project_name=project_name,
            summary=summary,
            final_goal=final_goal,
            roles=roles,
        )

    def _render_prior_outputs(self, stage_results: list[RoleRunResult]) -> str:
        chunks: list[str] = []
        for result in stage_results:
            if not result.enabled:
                continue
            if not result.output.strip():
                continue
            label = _ROLE_LABELS.get(result.role, result.role)
            chunks.append(f"[{label}]\n{self._clip_text(result.output)}")
        if not chunks:
            return "- (none)"
        return "\n\n".join(chunks)

    def _render_stage_results(self, stage_results: list[RoleRunResult]) -> str:
        lines: list[str] = []
        for result in stage_results:
            label = _ROLE_LABELS.get(result.role, result.role)
            if not result.enabled:
                lines.append(f"- {label}: skipped")
                continue
            artifacts = ", ".join(result.artifacts) if result.artifacts else "-"
            missing = ", ".join(result.missing_outputs) if result.missing_outputs else "-"
            lines.append(
                (
                    f"- {label}: artifacts={artifacts}; "
                    f"missing_required_outputs={missing}; "
                    f"output={self._clip_text(result.output)}"
                )
            )
        return "\n".join(lines)

    def _build_summary_line(self, stage_results: list[RoleRunResult]) -> str:
        executed = [result.role for result in stage_results if result.enabled]
        missing = [result.role for result in stage_results if result.enabled and result.missing_outputs]
        executed_text = ",".join(executed) if executed else "-"
        return (
            f"[multi-workflow] roles={executed_text}, "
            f"missing_output_roles={len(missing)}"
        )

    def _build_fallback_output(self, plan: WorkflowPlan, stage_results: list[RoleRunResult]) -> str:
        completed = [_ROLE_LABELS.get(result.role, result.role) for result in stage_results if result.enabled]
        completed_text = ", ".join(completed) if completed else "-"
        return (
            f"multi-agent workflow completed for '{plan.project_name}'. "
            f"executed roles: {completed_text}."
        )

    def _find_missing_outputs(
        self,
        required_outputs: tuple[str, ...],
        profile_working_directory: str | None,
    ) -> list[str]:
        if not required_outputs:
            return []
        workspace_root = self._resolve_workspace_root(profile_working_directory)
        missing: list[str] = []
        for raw_path in required_outputs:
            candidate = Path(raw_path)
            if not candidate.is_absolute():
                candidate = workspace_root / raw_path
            if not candidate.exists():
                missing.append(raw_path)
        return missing

    def _snapshot_workspace(self, profile_working_directory: str | None) -> dict[str, tuple[int, int]]:
        root = self._resolve_workspace_root(profile_working_directory)
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

    def _resolve_workspace_root(self, profile_working_directory: str | None) -> Path:
        if profile_working_directory:
            return Path(profile_working_directory).expanduser().resolve()
        return (self.workspace_dir or Path.cwd()).resolve()

    @staticmethod
    def _normalize_outputs(raw_outputs: Any) -> list[str]:
        if not isinstance(raw_outputs, list):
            return []
        normalized: list[str] = []
        for item in raw_outputs:
            cleaned = str(item).strip()
            if not cleaned:
                continue
            if cleaned not in normalized:
                normalized.append(cleaned)
        return normalized

    @staticmethod
    def _clip_text(text: str) -> str:
        cleaned = text.strip()
        if len(cleaned) <= _MAX_ROLE_OUTPUT_CHARS:
            return cleaned
        return cleaned[:_MAX_ROLE_OUTPUT_CHARS] + "..."

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
            cleaned.append({"role": role, "content": content})
        if len(cleaned) > _MAX_HISTORY_ITEMS:
            cleaned = cleaned[-_MAX_HISTORY_ITEMS:]
        return cleaned


def _extract_json_dict(raw: str) -> dict[str, Any] | None:
    cleaned = raw.strip()
    if not cleaned:
        return None

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


def _select_agent_override(mapping: dict[str, str], keys: tuple[str, ...]) -> str | None:
    for key in keys:
        value = mapping.get(key)
        if isinstance(value, str):
            cleaned = value.strip()
            if cleaned:
                return cleaned
    return None
