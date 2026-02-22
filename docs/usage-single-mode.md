# Codex Orchestrator 사용법 (Single 전용)

Telegram 연동부터 실제 채팅 운영 절차까지는 `docs/telegram-integration-runbook.md`를 참고하세요.

## 1. 현재 구현 범위
- Python 기반 오케스트레이터 핵심 로직 구현 완료
- `single` 모드 기본값 적용
- 명령 라우팅: `/start`, `/mode single`, `/new`, `/status`, `/cancel`, 그 외 `/...`, 일반 텍스트
- 세션 파일 저장, trace 로그 저장, Codex MCP 상태 조회 포함
- 참고: Telegram long polling 실행 스크립트(`scripts/telegram_polling_runner.py`)가 포함되어 있으며, `BotOrchestrator.handle_message()`로도 동일 로직을 직접 호출할 수 있습니다.

## 2. 요구 환경
- Python 3.11+
- MCP Python SDK (`mcp` 패키지)
- `npx` 또는 `codex mcp-server`를 실행할 수 있는 환경
- (선택) Codex MCP 상태 조회 커맨드

설치 예시:
```bash
python3 -m pip install mcp python-dotenv
```

## 3. 빠른 실행
### 3.1 테스트
```bash
PYTHONPATH=src python3 -m unittest discover -s tests -p 'test_*.py' -q
```

### 3.2 환경 변수 설정 (선택)
- `TELEGRAM_BOT_TOKEN`: Telegram Bot API token
- `CODEX_CONF_PATH`: conf 파일 경로(기본 `~/.codex-orchestrator/conf.toml`)

실행 옵션은 `conf.toml`에서 관리합니다.

`conf.toml` 예시:
```toml
[telegram]
allowed_users = [123456789]

[telegram.polling]
poll_timeout = 30
loop_sleep_sec = 1
delete_webhook_on_start = true
drop_pending_updates = false
ignore_pending_updates_on_start = true
# allowed_chat_ids = [123456789, 987654321]
require_mcp_warmup = true
cancel_wait_timeout_sec = 5

[codex]
mcp_command = "npx"
mcp_args = "-y codex mcp-server"
mcp_client_timeout_seconds = 360000
allow_echo_executor = false
approval_policy = "never"
sandbox = "danger-full-access"
mcp_direct_status = true
# mcp_status_cmd = "bash -lc \"echo running=true,ready=true,pid=12345,uptime_sec=30\""
mcp_auto_detect_process = false
# agent_model = "gpt-5"
# agent_working_directory = "~/develop/ai-agent/codex-orchestrator"

[profile]
default = "bridge"

[profiles.default]
model = "gpt-5"
working_directory = "~/develop/ai-agent/codex-orchestrator"

[agents.single.planner]
model = "gpt-5"
system_prompt = "You are Planner Agent. Build concise implementation handoff."

[agents.single.developer]
model = "gpt-5-codex"
system_prompt_file = "./prompts/developer.txt"

[agents.single.reviewer]
system_prompt = "You are Reviewer Agent. Focus on concrete diffs and risks."

[profiles.bridge]
model = "gpt-5"
working_directory = "~/develop/bridge-project"
```
- 기본 경로(`~/.codex-orchestrator/conf.toml`) 파일이 없으면 runner 최초 실행 시 자동 생성됩니다.
- `allowed_users` 설정 시 목록에 없는 Telegram 사용자는 `Unauthorized` 응답 후 차단됩니다.
- `/profile <name>`으로 프로파일을 전환하면 `model`/`working_directory`와 agent별 override가 함께 적용됩니다.
- agent별 설정 키:
  - `agents.single.planner`
  - `agents.single.developer`
  - `agents.single.reviewer`
- 현재 agent 이름:
  - single 모드: `single.planner`, `single.developer`, `single.reviewer`
- agent별 값이 없으면 기본값을 사용합니다.
  - model 기본값: `profiles.<name>.model`
  - system prompt 기본값: single은 내장 기본 프롬프트

주의:
- 실행 경로는 MCP client + `codex` MCP tool 직접 호출입니다.
- `OPENAI_API_KEY`는 사용하지 않습니다.

### 3.3 로컬 호출 예시
```bash
PYTHONPATH=src python3 - <<'PY'
import asyncio
from main import build_orchestrator

async def main():
    bot = build_orchestrator()
    print(await bot.handle_message("100", "200", "/start"))
    print(await bot.handle_message("100", "200", "file 에 textbox를 추가해"))
    print(await bot.handle_message("100", "200", "/status"))

asyncio.run(main())
PY
```

## 4. 명령 사용법
- `/start`: 사용 가능한 명령 안내
- `/mode single`: single 모드로 전환
- `/new`: 현재 `chat_id:user_id` 세션 초기화 (모드도 `single`로 리셋)
- `/status`: single 모드 상태, 최근 실행 결과, single 리뷰 상태, codex_mcp 상태 출력
- `/cancel`: 현재 세션에서 실행 중인 요청 취소
- `/profile list|<name>`: 프로파일 목록 조회/전환
- 일반 텍스트: single 워크플로우로 즉시 전달

라우팅 규칙:
- 예약 명령(`/start`, `/mode`, `/new`, `/status`, `/cancel`, `/profile`)만 내부 처리
- 그 외 `/...`는 Codex 슬래시 명령으로 전달

## 5. Single 모드 동작
Single 모드는 `Planner -> Developer -> Reviewer` 단계로 동작합니다.
1. Planner가 사용자 요청 기준의 구현 계획(handoff)을 생성
2. Developer가 계획과 요청을 기반으로 구현/수정 수행
3. Reviewer가 승인(`approved`) 또는 수정요청(`needs_changes`) 판단
4. 수정요청이면 Reviewer 피드백을 반영해 Developer/Reviewer 단계를 반복
5. 승인 또는 최대 3회 반복 시 종료

최종 응답에 아래 요약이 포함됩니다.
- `[single-review] stages=planner>developer>reviewer, rounds=<n>/3, result=<approved|max_rounds_reached>`

## 6. 상태/로그 파일
### 6.1 Session 파일
- 경로: `~/.codex-orchestrator/sessions/`
- 파일명: `{chatId}-{userId}.json`
- 예: `~/.codex-orchestrator/sessions/100-200.json`

### 6.2 Trace 파일
- 경로: `~/.codex-orchestrator/traces/`
- 파일명: `{yyyy-mm-dd}.jsonl`
- 1 요청당 1 line append
- 민감 정보(`token`, `api_key`, `authorization`) 마스킹 저장

## 7. `/status` 출력 예시
```text
mode: single
profile: bridge, model=gpt-5, working_directory=/home/user/develop/bridge-project
last_run: ok (4200ms)
single_review: rounds=2/3, result=approved
codex_mcp: running=true, ready=true, pid=12345, uptime=532s
last_error: -
```

## 8. Codex MCP 상태 커맨드 형식
`codex.mcp_status_cmd` 출력은 다음 중 하나를 지원합니다.
- JSON: `{"running":true,"ready":true,"pid":12345,"uptime_sec":120}`
- key=value CSV: `running=true,ready=true,pid=12345,uptime_sec=120`

커맨드 실패 시 `/status`는 `codex_mcp: unknown`으로 표시됩니다.
