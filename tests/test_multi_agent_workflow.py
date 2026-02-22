import asyncio
import json
import tempfile
import unittest
from pathlib import Path

from core.models import BotSession
from workflows.multi_agent_workflow import MultiAgentWorkflow


class _SequencedExecutor:
    def __init__(self, responses: list[str]) -> None:
        self._responses = list(responses)
        self.calls: list[dict[str, str | None]] = []

    async def run(self, prompt, history=None, *, system_instructions=None, model=None, cwd=None):
        self.calls.append(
            {
                "prompt": prompt,
                "model": model,
                "cwd": cwd,
                "system_instructions": system_instructions,
            }
        )
        if self._responses:
            return self._responses.pop(0)
        return "ok"


class MultiAgentWorkflowTests(unittest.TestCase):
    @staticmethod
    def _plan_payload(
        *,
        enabled_roles: dict[str, bool],
        required_outputs: dict[str, list[str]] | None = None,
    ) -> str:
        required_outputs = required_outputs or {}
        payload = {
            "project_name": "Bug Busters",
            "summary": "Build a tiny browser game.",
            "final_goal": "Deliver working frontend/backend/test artifacts.",
            "roles": {
                "designer": {
                    "enabled": enabled_roles.get("designer", False),
                    "task": "Create UI/UX spec.",
                    "required_outputs": required_outputs.get("designer", []),
                },
                "frontend_developer": {
                    "enabled": enabled_roles.get("frontend_developer", False),
                    "task": "Implement frontend page and game loop.",
                    "required_outputs": required_outputs.get("frontend_developer", []),
                },
                "backend_developer": {
                    "enabled": enabled_roles.get("backend_developer", False),
                    "task": "Implement GET /health and GET/POST /scores.",
                    "required_outputs": required_outputs.get("backend_developer", []),
                },
                "tester": {
                    "enabled": enabled_roles.get("tester", False),
                    "task": "Verify acceptance criteria and routes.",
                    "required_outputs": required_outputs.get("tester", []),
                },
            },
        }
        return json.dumps(payload)

    def test_profile_model_and_working_directory_are_forwarded(self) -> None:
        executor = _SequencedExecutor(
            [
                self._plan_payload(
                    enabled_roles={
                        "designer": False,
                        "frontend_developer": False,
                        "backend_developer": False,
                        "tester": False,
                    }
                ),
                "final-summary",
            ]
        )
        workflow = MultiAgentWorkflow(executor=executor)
        session = BotSession(session_id="tg:1:2", chat_id="1", user_id="2")
        session.profile_model = "gpt-5"
        session.profile_working_directory = "/tmp/bridge"

        result = asyncio.run(workflow.run("hello", session))

        self.assertIn("final-summary", result["output_text"])
        self.assertIn("[multi-workflow]", result["output_text"])
        self.assertEqual(len(executor.calls), 2)
        self.assertEqual(executor.calls[0]["model"], "gpt-5")
        self.assertEqual(executor.calls[1]["model"], "gpt-5")
        self.assertEqual(executor.calls[0]["cwd"], "/tmp/bridge")
        self.assertEqual(executor.calls[1]["cwd"], "/tmp/bridge")
        self.assertIsNotNone(executor.calls[0]["system_instructions"])

    def test_multi_agent_role_overrides_are_forwarded(self) -> None:
        executor = _SequencedExecutor(
            [
                self._plan_payload(
                    enabled_roles={
                        "designer": True,
                        "frontend_developer": True,
                        "backend_developer": True,
                        "tester": True,
                    }
                ),
                "designer-output",
                "frontend-output",
                "backend-output",
                "tester-output",
                "manager-final-output",
            ]
        )
        workflow = MultiAgentWorkflow(executor=executor)
        session = BotSession(session_id="tg:1:2", chat_id="1", user_id="2")
        session.profile_model = "gpt-5-default"
        session.profile_working_directory = "/tmp/bridge"
        session.profile_agent_models = {
            "multi.manager": "gpt-5-manager",
            "multi.designer": "gpt-5-designer",
            "multi.frontend.developer": "gpt-5-frontend",
            "multi.backend.developer": "gpt-5-backend",
            "multi.tester": "gpt-5-tester",
        }
        session.profile_agent_system_prompts = {
            "multi.manager": "Manager custom system prompt",
            "multi.designer": "Designer custom system prompt",
            "multi.frontend.developer": "Frontend custom system prompt",
            "multi.backend.developer": "Backend custom system prompt",
            "multi.tester": "Tester custom system prompt",
        }

        result = asyncio.run(workflow.run("hello", session))

        self.assertIn("manager-final-output", result["output_text"])
        self.assertEqual(len(executor.calls), 6)
        self.assertEqual(executor.calls[0]["model"], "gpt-5-manager")
        self.assertEqual(executor.calls[1]["model"], "gpt-5-designer")
        self.assertEqual(executor.calls[2]["model"], "gpt-5-frontend")
        self.assertEqual(executor.calls[3]["model"], "gpt-5-backend")
        self.assertEqual(executor.calls[4]["model"], "gpt-5-tester")
        self.assertEqual(executor.calls[5]["model"], "gpt-5-manager")
        self.assertEqual(executor.calls[0]["system_instructions"], "Manager custom system prompt")
        self.assertEqual(executor.calls[1]["system_instructions"], "Designer custom system prompt")
        self.assertEqual(executor.calls[2]["system_instructions"], "Frontend custom system prompt")
        self.assertEqual(executor.calls[3]["system_instructions"], "Backend custom system prompt")
        self.assertEqual(executor.calls[4]["system_instructions"], "Tester custom system prompt")
        self.assertEqual(executor.calls[5]["system_instructions"], "Manager custom system prompt")

    def test_missing_required_outputs_trigger_retry(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            executor = _SequencedExecutor(
                [
                    self._plan_payload(
                        enabled_roles={
                            "designer": True,
                            "frontend_developer": False,
                            "backend_developer": False,
                            "tester": False,
                        },
                        required_outputs={"designer": ["design/design_spec.md"]},
                    ),
                    "designer-first-attempt",
                    "designer-second-attempt",
                    "manager-final-output",
                ]
            )
            workflow = MultiAgentWorkflow(executor=executor)
            session = BotSession(session_id="tg:1:2", chat_id="1", user_id="2")
            session.profile_working_directory = str(workspace)

            result = asyncio.run(workflow.run("hello", session))

        self.assertEqual(len(executor.calls), 4)
        self.assertIn("still missing", str(executor.calls[2]["prompt"]).lower())
        self.assertIn("missing_output_roles=1", result["output_text"])
        metadata = result.get("metadata", {})
        stages = metadata.get("stages", []) if isinstance(metadata, dict) else []
        designer_stage = next(stage for stage in stages if stage.get("role") == "designer")
        self.assertEqual(designer_stage.get("missing_outputs"), ["design/design_spec.md"])


if __name__ == "__main__":
    unittest.main()
