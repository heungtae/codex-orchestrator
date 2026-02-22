import subprocess
import unittest
from unittest.mock import patch

from integrations.codex_mcp import CodexMcpServer


class CodexMcpServerTests(unittest.TestCase):
    def test_get_status_uses_configured_status_command(self) -> None:
        server = CodexMcpServer(status_command=["dummy", "status"])

        completed = subprocess.CompletedProcess(
            args=["dummy", "status"],
            returncode=0,
            stdout="running=true,ready=true,pid=4242,uptime_sec=15",
            stderr="",
        )

        with patch("subprocess.run", return_value=completed):
            status = server.get_status()

        self.assertTrue(status["running"])
        self.assertTrue(status["ready"])
        self.assertEqual(status["pid"], 4242)
        self.assertEqual(status["uptime_sec"], 15)

    def test_get_status_auto_detects_mcp_server_process(self) -> None:
        server = CodexMcpServer(status_command=None, process_match_query="codex mcp-server")

        completed = subprocess.CompletedProcess(
            args=["ps", "-eo", "pid=,etimes=,args="],
            returncode=0,
            stdout=(
                "101 10 /usr/bin/python worker.py\n"
                "222 35 codex mcp-server -c model=\"o3\"\n"
            ),
            stderr="",
        )

        with patch("subprocess.run", return_value=completed):
            status = server.get_status()

        self.assertTrue(status["running"])
        self.assertTrue(status["ready"])
        self.assertEqual(status["pid"], 222)
        self.assertEqual(status["uptime_sec"], 35)

    def test_get_status_reports_not_running_when_process_missing(self) -> None:
        server = CodexMcpServer(status_command=None, process_match_query="codex mcp-server")

        completed = subprocess.CompletedProcess(
            args=["ps", "-eo", "pid=,etimes=,args="],
            returncode=0,
            stdout="",
            stderr="",
        )

        with patch("subprocess.run", return_value=completed):
            status = server.get_status()

        self.assertFalse(status["running"])
        self.assertFalse(status["ready"])
        self.assertIsNone(status["pid"])
        self.assertIsNone(status["uptime_sec"])


if __name__ == "__main__":
    unittest.main()
