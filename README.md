# codex-orchestrator

A Telegram-bot-driven orchestrator for running Codex in single-run mode and plan workflow mode. It receives updates via Telegram long polling and forwards operational commands such as `/mode`, `/profile`, and `/cancel` into the pipeline. It also tracks Codex MCP execution status and stores sessions and trace logs locally.

## Key Features
- Routes Telegram long-polling updates and operational commands such as `/mode`, `/profile`, and `/cancel` into the orchestrator pipeline.
- Runs a fast-response developer agent in single mode, and executes a `selector -> planner -> developer -> reviewer` flow in plan mode with up to three review rounds.
- Restricts access using `telegram.allowed_users`, so only authorized users can run Codex workflows.
- Warms up the Codex MCP executor and periodically checks status for stable operation.
- Stores per-user session data and daily trace files locally, so state can continue after restart and support debugging.
- Includes timestamps in stdout logs for clearer operational event tracing.

## Project Structure
- `src/core`: request routing, orchestration, session/profile management, trace logging
- `src/workflows`: single/plan workflow logic and execution orchestration
- `src/integrations`: Codex executor and MCP status integration layer
- `src/bot`: Telegram update parsing and message splitting/cleanup
- `scripts/telegram_polling_runner.py`: operational entry point that receives Telegram messages via long polling
- `tests`: `unittest` coverage for command routing, workflow transitions, and integration boundaries (Executor/MCP status)

## Requirements
- Python 3.10+
- Environment capable of running `npx` and `codex mcp-server` (for MCP communication)
- Telegram bot token (`TELEGRAM_BOT_TOKEN` environment variable)

## Installation

### Install from PyPI
```bash
python3 -m pip install codex_orchestrator
```

### Install a specific release
```bash
python3 -m pip install "codex_orchestrator==<version>"
# Example: python3 -m pip install "codex_orchestrator==0.1.4"
```

### Change version (upgrade/downgrade)
```bash
python3 -m pip install --upgrade "codex_orchestrator==<version>"
```

### Resolve `error: externally-managed-environment` on Ubuntu/Debian
Option 1 (user-local install)
```bash
python3 -m pip install --user --break-system-packages codex_orchestrator
```
Binaries are installed to `~/.local/bin`, so update PATH.
```bash
echo 'export PATH="$HOME/.local/bin:$PATH"' >> ~/.bashrc
source ~/.bashrc
hash -r
```

Option 2 (virtual environment)
```bash
python3 -m venv .venv
source .venv/bin/activate
python3 -m pip install -U pip
python3 -m pip install codex_orchestrator
```

### Development setup (run from source)
```bash
python3 -m pip install mcp python-dotenv
```

## User Setup
1. Copy the environment template.
```bash
cp .env.example .env
```
2. Create the user config directory and copy the default config file.
```bash
mkdir -p ~/.codex-orchestrator
cp conf.toml.example ~/.codex-orchestrator/conf.toml
```
3. Update required settings.
- Set your Telegram bot token in `.env` under `TELEGRAM_BOT_TOKEN`.
- Set allowed Telegram user IDs in `~/.codex-orchestrator/conf.toml` under `telegram.allowed_users`.
- Adjust runtime options such as `codex.*` and `telegram.polling.*` in `conf.toml`.

Notes:
- To use a different config file, set `CODEX_CONF_PATH` to the desired path.
- Path values such as `working_directory` and `system_prompt_file` are resolved relative to the `conf.toml` location.

## Run
If installed from PyPI:
```bash
codex-orchestrator
```
If the command is not found, run the binary directly.
```bash
~/.local/bin/codex-orchestrator
```

To run from local source:
```bash
PYTHONPATH=src python3 scripts/telegram_polling_runner.py
```

## Tests
Run the full test suite:
```bash
PYTHONPATH=src python3 -m unittest discover -s tests -p 'test_*.py' -q
```

Run a specific module:
```bash
PYTHONPATH=src python3 -m unittest -q tests.test_telegram_polling_runner
```

## Telegram Commands
- `/start`: show usage and command list
- `/mode single|plan`: switch between single mode and plan mode (default: plan)
- `/new`: reset the current session
- `/status`: report current execution status
- `/cancel`: cancel the active request or workflow
- `/profile list|<name>`: list available profiles or switch to the specified profile

## Runtime Files
- Session files: `~/.codex-orchestrator/sessions/{chatId}-{userId}.json`
- Trace files: `~/.codex-orchestrator/traces/{yyyy-mm-dd}.jsonl`

## Additional Docs
- `docs/telegram-integration-runbook.md`: Telegram integration operations guide
- `docs/usage-single-mode.md`: single-mode usage guide
