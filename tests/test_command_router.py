import unittest

from core.command_router import CommandRouter


class CommandRouterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.router = CommandRouter()

    def test_reserved_commands_are_parsed_as_bot_commands(self) -> None:
        route = self.router.route("/mode single")
        self.assertEqual(route.kind, "bot_command")
        self.assertEqual(route.command, "mode")
        self.assertEqual(route.args, ("single",))

        route = self.router.route("/new")
        self.assertEqual(route.kind, "bot_command")
        self.assertEqual(route.command, "new")

        route = self.router.route("/status")
        self.assertEqual(route.kind, "bot_command")
        self.assertEqual(route.command, "status")

        route = self.router.route("/profile list")
        self.assertEqual(route.kind, "bot_command")
        self.assertEqual(route.command, "profile")
        self.assertEqual(route.args, ("list",))

        route = self.router.route("/cancel")
        self.assertEqual(route.kind, "bot_command")
        self.assertEqual(route.command, "cancel")

        route = self.router.route("/cancel@my_bot")
        self.assertEqual(route.kind, "bot_command")
        self.assertEqual(route.command, "cancel")

    def test_non_reserved_slash_command_is_forwarded_to_codex(self) -> None:
        route = self.router.route("/edit add textbox")
        self.assertEqual(route.kind, "codex_slash")
        self.assertEqual(route.text, "/edit add textbox")

    def test_codex_literal_is_forwarded_as_non_reserved_slash(self) -> None:
        route = self.router.route("/codex /status")
        self.assertEqual(route.kind, "codex_slash")
        self.assertEqual(route.text, "/codex /status")

    def test_plain_text_is_forwarded(self) -> None:
        route = self.router.route("add a textbox to the file")
        self.assertEqual(route.kind, "text")
        self.assertEqual(route.text, "add a textbox to the file")


if __name__ == "__main__":
    unittest.main()
