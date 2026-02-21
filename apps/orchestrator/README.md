# Codex Orchestrator App

Telegram 명령으로 Codex MCP 실행을 제어하는 TypeScript 오케스트레이터입니다.

## Commands
- `/run <instruction...>`: 새 세션 실행 (`codex`)
- `/reply <instruction...>`: 기존 세션 이어서 실행 (`codex-reply`)
- `/status`: 마지막 job 상태 조회
- `/tail [n]`: 최신 로그 마지막 N줄 조회

## Local Run
```bash
cd apps/orchestrator
npm install
npm run build
npm start
```

개발 실행:
```bash
cd apps/orchestrator
npm run dev
```

## Required Environment Variables
- `TELEGRAM_BOT_TOKEN`: Telegram bot token

## Optional Environment Variables
- `TELEGRAM_ALLOWED_CHAT_ID`: 허용할 chat id (미설정 시 전체 허용)
- `STATE_DIR` (default: `.state`)
- `CODEX_WORKSPACE_CWD` (default: current working directory)
- `CODEX_SANDBOX` (default: `workspace-write`)
- `CODEX_APPROVAL_POLICY` (default: `on-request`)
- `CODEX_TIMEOUT_MS` (default: `600000`)
- `MCP_RUN_COMMAND` (default: `codex mcp call codex`)
- `MCP_REPLY_COMMAND` (default: `codex mcp call codex-reply`)
- `STALE_LOCK_OVERRIDE_MINUTES` (default: `0`)
- `TAIL_DEFAULT_LINES` (default: `80`)
- `TAIL_MAX_LINES` (default: `300`)
- `TELEGRAM_POLL_TIMEOUT_SECONDS` (default: `25`)
- `TELEGRAM_POLL_INTERVAL_MS` (default: `1500`)

## MCP Command Input
기본 동작은 MCP 명령에 JSON payload를 stdin으로 전달합니다.

payload placeholder를 명령 템플릿에 넣고 싶으면 `{{payload}}`를 사용하세요.
예시:
```bash
MCP_RUN_COMMAND='my-mcp-run --json {{payload}}'
```
