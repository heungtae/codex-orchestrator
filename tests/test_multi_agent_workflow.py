import asyncio
import unittest

from core.models import BotSession
from workflows.multi_agent_workflow import MultiAgentWorkflow


class _CapturingExecutor:
    def __init__(self) -> None:
        self.calls: list[dict[str, str | None]] = []

    async def run(self, prompt, history=None, *, system_instructions=None, model=None, cwd=None):
        self.calls.append({"model": model, "cwd": cwd})
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


if __name__ == "__main__":
    unittest.main()
