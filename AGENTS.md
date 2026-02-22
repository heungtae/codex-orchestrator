# Repository Guidelines

## Project Structure & Module Organization
Core application code lives in `src/`:
- `src/core/`: orchestrator, routing, sessions, profiles, trace logging.
- `src/workflows/`: single/multi agent workflow logic and factory.
- `src/integrations/`: Codex MCP executor/status integration.
- `src/bot/`: Telegram update parsing and message splitting.

Operational entry points are in `scripts/` (notably `scripts/telegram_polling_runner.py`).
Tests are in `tests/` with `test_*.py` naming. Supporting docs and runbooks are in `docs/`.

## Build, Test, and Development Commands
- `python3 -m pip install mcp python-dotenv`: install runtime dependencies used by local runs.
- `PYTHONPATH=src python3 -m unittest discover -s tests -p 'test_*.py' -q`: run full test suite.
- `PYTHONPATH=src python3 -m unittest -q tests.test_telegram_polling_runner`: run one focused test module.
- `PYTHONPATH=src python3 scripts/telegram_polling_runner.py`: start Telegram long-polling runner.

Use `PYTHONPATH=src` consistently so imports resolve without packaging steps.

## Coding Style & Naming Conventions
Follow Python 3.11+ conventions:
- 4-space indentation, PEP 8 naming (`snake_case` functions/variables, `CamelCase` classes).
- Keep modules focused by layer (`core`, `workflows`, `integrations`, `bot`).
- Prefer explicit type hints and dataclasses where state is structured.
- Keep comments minimal and practical; explain non-obvious behavior only.

## Testing Guidelines
- Framework: `unittest` (pytest-compatible layout is configured in `pyproject.toml`).
- Add tests in `tests/test_<feature>.py` and keep test names behavior-focused (for example `test_profile_switch_updates_executor_context`).
- Cover command routing, workflow transitions, and integration boundaries (MCP status parsing, Telegram update handling).
- Run relevant module tests before committing, then run full suite.

## Commit & Pull Request Guidelines
- Use Conventional Commits where possible: `feat(...)`, `fix(...)`, `docs`, `refactor`, etc.
- Keep one logical change per commit and include tests with behavior changes.
- PRs should include:
  1. What changed and why.
  2. How it was tested (commands + result).
  3. Any config/env updates (for example `CODEX_CONF_PATH`, `TELEGRAM_BOT_TOKEN`).

## Security & Configuration Tips
- Never commit secrets or tokens; configure via environment variables.
- Treat `conf.toml` and files under `~/.codex-orchestrator/` as local runtime state.
- Do not commit local logs/artifacts such as `codex-notification.txt`.
