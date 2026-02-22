import asyncio
import unittest

from core.models import BotSession
from workflows.multi_agent_workflow import MultiAgentWorkflow


class _CapturingExecutor:
    def __init__(self) -> None:
        self.calls: list[dict[str, str | None]] = []

    async def run(self, prompt, history=None, *, system_instructions=None, model=None, cwd=None):
        self.calls.append(
            {"model": model, "cwd": cwd, "system_instructions": system_instructions}
        )
        return "ok"


class MultiAgentWorkflowTests(unittest.TestCase):
    def test_profile_model_and_working_directory_are_forwarded(self) -> None:
        executor = _CapturingExecutor()
        workflow = MultiAgentWorkflow(executor=executor)
        session = BotSession(session_id="tg:1:2", chat_id="1", user_id="2")
        session.profile_model = "gpt-5"
        session.profile_working_directory = "/tmp/bridge"

        result = asyncio.run(workflow.run("hello", session))

        self.assertEqual(result["output_text"], "ok")
        self.assertEqual(len(executor.calls), 1)
        self.assertEqual(executor.calls[0]["model"], "gpt-5")
        self.assertEqual(executor.calls[0]["cwd"], "/tmp/bridge")
        self.assertIsNone(executor.calls[0]["system_instructions"])

    def test_multi_agent_overrides_are_forwarded(self) -> None:
        executor = _CapturingExecutor()
        workflow = MultiAgentWorkflow(executor=executor)
        session = BotSession(session_id="tg:1:2", chat_id="1", user_id="2")
        session.profile_model = "gpt-5"
        session.profile_working_directory = "/tmp/bridge"
        session.profile_agent_models = {"multi.manager": "gpt-5-multi"}
        session.profile_agent_system_prompts = {"multi.manager": "Multi custom system prompt"}

        result = asyncio.run(workflow.run("hello", session))

        self.assertEqual(result["output_text"], "ok")
        self.assertEqual(len(executor.calls), 1)
        self.assertEqual(executor.calls[0]["model"], "gpt-5-multi")
        self.assertEqual(executor.calls[0]["cwd"], "/tmp/bridge")
        self.assertEqual(executor.calls[0]["system_instructions"], "Multi custom system prompt")


if __name__ == "__main__":
    unittest.main()
