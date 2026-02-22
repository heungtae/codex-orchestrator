import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from main import build_orchestrator


class MainBuildOrchestratorTests(unittest.TestCase):
    def test_direct_status_is_default(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env = {"CODEX_CONF_PATH": str(Path(tmp) / "conf.toml")}
            with patch.dict("os.environ", env, clear=True):
                orchestrator = build_orchestrator()

        self.assertIsNone(orchestrator.codex_mcp.status_command)
        self.assertFalse(orchestrator.codex_mcp.auto_detect_process)

    def test_external_status_mode_can_be_enabled(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env = {
                "CODEX_CONF_PATH": str(Path(tmp) / "conf.toml"),
                "CODEX_MCP_DIRECT_STATUS": "false",
                "CODEX_MCP_STATUS_CMD": "echo running=true",
                "CODEX_MCP_AUTO_DETECT_PROCESS": "true",
            }
            with patch.dict("os.environ", env, clear=True):
                orchestrator = build_orchestrator()

        self.assertEqual(orchestrator.codex_mcp.status_command, ["echo", "running=true"])
        self.assertTrue(orchestrator.codex_mcp.auto_detect_process)


if __name__ == "__main__":
    unittest.main()
