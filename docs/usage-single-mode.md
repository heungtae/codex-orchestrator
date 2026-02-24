# Codex Orchestrator 사용법

Telegram 연동부터 실제 채팅 운영 절차까지는 `docs/telegram-integration-runbook.md`를 참고하세요.

## 1. 현재 구현 범위
- Python 기반 오케스트레이터 핵심 로직 구현 완료
- `plan` 모드 기본값 적용
- 명령 라우팅: `/start`, `/mode`, `/new`, `/status`, `/cancel`, 그 외 일반 텍스트
- 세션 파일 저장, trace 로그 저장, Codex MCP 상태 조회 포함
- plan 모드: selector가 요청을 분석하여 single/plan 자동 라우팅

## 2. 요구 환경
- Python 3.10+
- MCP Python SDK (`mcp` 패키지)
- `npx` 또는 `codex mcp-server`를 실행할 수 있는 환경

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
mcp_auto_detect_process = false

[profile]
default = "default"

[profiles.default]
model = "gpt-5"
working_directory = "~/develop/ai-agent/codex-orchestrator"

# If both system_prompt and system_prompt_file are set, system_prompt_file takes precedence.

# single mode: direct developer execution
[agents.single.developer]
model = "gpt-5-codex"
system_prompt = "You are Single Developer Agent. Implement user requests directly."
system_prompt_file = "./prompts/developer.txt"

# plan mode: selector -> planner -> developer -> reviewer
[agents.plan.selector]
model = "gpt-5"
system_prompt = "You are Plan Selector Agent. Analyze requests and select execution mode."

[agents.plan.planner]
model = "gpt-5"
system_prompt = "You are Plan Planner Agent. Create execution plans."

[agents.plan.developer]
model = "gpt-5-codex"
system_prompt = "You are Plan Developer Agent. Implement code based on plans."

[agents.plan.reviewer]
model = "gpt-5"
system_prompt = "You are Plan Reviewer Agent. Review and approve implementation."
```
- 기본 경로(`~/.codex-orchestrator/conf.toml`) 파일이 없으면 runner 최초 실행 시 자동 생성됩니다.
- `allowed_users` 설정 시 목록에 없는 Telegram 사용자는 `Unauthorized` 응답 후 차단됩니다.
- `/profile <name>`으로 프로파일을 전환하면 `model`/`working_directory`와 agent별 override가 함께 적용됩니다.
- agent 설정 키:
  - single: `agents.single.developer`
  - plan: `agents.plan.selector`, `agents.plan.planner`, `agents.plan.developer`, `agents.plan.reviewer`
- agent별 값이 없으면 기본값을 사용합니다.
  - model 기본값: `profiles.<name>.model`
  - system prompt 기본값: 내장 기본 프롬프트

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
- `/mode single|plan`: 모드 전환 (기본: plan)
- `/new`: 현재 `chat_id:user_id` 세션 초기화 (모드도 `plan`으로 리셋)
- `/status`: 현재 모드 상태, 최근 실행 결과, codex_mcp 상태 출력
- `/cancel`: 현재 세션에서 실행 중인 요청 취소
- `/profile list|<name>`: 프로파일 목록 조회/전환
- 일반 텍스트: plan 워크플로우로 전달 (selector가 single/plan 자동 결정)

라우팅 규칙:
- 예약 명령(`/start`, `/mode`, `/new`, `/status`, `/cancel`, `/profile`)만 내부 처리
- 그 외 `/...`는 Codex 슬래시 명령으로 전달

## 5. Single/Plan 모드 동작

### Single 모드
Single 모드는 단일 developer agent가 사용자 요청을 즉시 실행합니다.
- selector/planner/reviewer 단계를 거치지 않습니다.
- 응답에는 review 요약 라인을 붙이지 않습니다.
- agent 이름: `single.developer`

### Plan 모드
Plan 모드는 selector가 요청을 분석하여 실행 모드를 결정합니다:
1. **Selector**: 요청을 분석하여 `single` 또는 `plan` 모드 결정
   - 단순 요청(질문, 파일 조회, 소규모 수정) → single로 위임
   - 복잡한 요청(새 기능, 리팩토링, 다중 파일 변경) → plan으로 진행
2. **Planner** (plan 모드만): 실행 계획 생성
3. **Developer**: 코드 실행
4. **Reviewer** (plan 모드만): 결과 검토 (최대 3회)

Plan 모드 출력 예시:
```
[Selector] mode=plan, reason=Multi-file implementation requires planning

[plan-workflow] rounds=1/3, result=approved
<실행 결과>
```

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

### Single 모드
```text
[Status]:
mode=single
profile=bridge, model=gpt-5, working_directory=/home/user/develop/bridge-project
last_run=ok (1200ms)
single_run=direct
codex_mcp=running=true, ready=true, pid=12345, uptime=532s
last_error=-
```

### Plan 모드
```text
[Status]:
mode=plan
profile=default, model=gpt-5, working_directory=/home/user/project
last_run=ok (3500ms)
plan_review=rounds=2/3, result=approved
codex_mcp=running=true, ready=true, pid=12345, uptime=120s
last_error=-
```

## 8. Codex MCP 상태 커맨드 형식
`codex.mcp_status_cmd` 출력은 다음 중 하나를 지원합니다.
- JSON: `{"running":true,"ready":true,"pid":12345,"uptime_sec":120}`
- key=value CSV: `running=true,ready=true,pid=12345,uptime_sec=120`

커맨드 실패 시 `/status`는 `codex_mcp=unknown`으로 표시됩니다.

## 9. Telegram 메시지 표준화
모든 Telegram 응답은 표준화된 형식을 사용합니다:
- `[Commands]:` - 명령어 목록
- `[Current]:` - 현재 모드/디렉토리
- `[Status]:` - 실행 상태
- `[Profiles]:` - 프로파일 목록
- `[Mode]:` - 모드 변경 결과
- `[Profile]:` - 프로파일 변경 결과
- `[Cancel]:` - 취소 결과
- `[Error]:` - 오류 메시지

key=value 형식으로统一됩니다.
