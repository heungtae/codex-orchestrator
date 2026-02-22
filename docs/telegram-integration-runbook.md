# Telegram 연동 및 codex-orchestrator 실행 가이드

이 문서는 아래 순서로 진행합니다.
1. Telegram Bot 생성
2. 로컬에서 codex-orchestrator 실행
3. Telegram 채팅으로 Codex 워크플로우 사용

## 1) 사전 준비
- Python 3.11+
- 이 레포 루트 경로
- Telegram 계정
- MCP Python SDK 설치

```bash
python3 -m pip install mcp python-dotenv
```

## 2) Telegram Bot 생성 (BotFather)
1. Telegram에서 `@BotFather`를 연다.
2. `/newbot` 실행 후 bot 이름/username을 등록한다.
3. 발급된 HTTP API token을 복사한다. (예: `123456:ABC...`)

선택 설정:
- 그룹에서 메시지를 모두 받으려면 BotFather의 `/setprivacy`를 `Disable`로 설정한다.
- bot 명령 목록은 `/setcommands`로 등록 가능하다.

## 3) 환경 변수 설정
레포 루트에서 아래를 설정한다.

```bash
export TELEGRAM_BOT_TOKEN='발급받은_토큰'

# 선택: Codex MCP 실행 커맨드/인자
export CODEX_MCP_COMMAND='npx'
export CODEX_MCP_ARGS='-y codex mcp-server'
export CODEX_MCP_CLIENT_TIMEOUT_SECONDS='360000'

# 선택: Codex 모델 오버라이드
# export CODEX_AGENT_MODEL='gpt-5'

# 선택: MCP 상태 조회 커맨드
export CODEX_MCP_STATUS_CMD='bash -lc "echo running=true,ready=true,pid=12345,uptime_sec=30"'

# 선택: 허용 chat_id 제한 (콤마 구분)
# export TELEGRAM_ALLOWED_CHAT_IDS='123456789,987654321'

# 디버그 전용: Echo 실행기 강제
# export CODEX_ALLOW_ECHO_EXECUTOR='true'
```

`CODEX_MCP_STATUS_CMD`를 설정하지 않으면 `ps` 기반으로 `codex mcp-server` 프로세스를 자동 탐지한다.

## 4) 실행 방법 (Polling)
이 레포에는 long polling 실행 스크립트가 포함되어 있다.

실행:
```bash
PYTHONPATH=src python3 scripts/telegram_polling_runner.py
```

옵션 환경 변수:
- `TELEGRAM_POLL_TIMEOUT` (기본 `30`): `getUpdates` long poll timeout
- `TELEGRAM_LOOP_SLEEP_SEC` (기본 `1`): 루프 사이 sleep
- `TELEGRAM_DELETE_WEBHOOK_ON_START` (기본 `true`): 시작 시 webhook 해제
- `TELEGRAM_DROP_PENDING_UPDATES` (기본 `false`): webhook 해제 시 대기 update 제거 여부
- `TELEGRAM_ALLOWED_CHAT_IDS`: 허용 chat id 목록
- `TELEGRAM_REQUIRE_MCP_WARMUP` (기본 `true`): 시작 시 MCP warmup 실패하면 프로세스 종료

참고:
- polling 사용 시 기존 webhook이 있으면 충돌할 수 있어 기본으로 `deleteWebhook`를 호출한다.

## 5) Telegram에서 채팅하는 방법
Telegram에서 생성한 bot과 대화를 시작한 뒤 아래처럼 사용한다.

### 5.1 기본 확인
- `/start`: 사용 가능한 명령 확인
- `/status`: 현재 모드/최근 실행/single 리뷰/codex_mcp 상태 확인

### 5.2 모드 제어
- `/mode single`: single 모드 전환 (기본값)
- `/mode multi`: multi 모드 전환
- `/new`: 현재 `chat_id:user_id` 세션 초기화 (mode=single)

### 5.3 실제 작업 요청
일반 텍스트를 그대로 보내면 Codex 워크플로우로 전달된다.

예시:
- `file 에 textbox를 추가해`
- `로그인 API 에러 원인 분석해줘`

슬래시 입력 규칙:
- 예약 명령(`/start`, `/mode`, `/new`, `/status`)은 bot이 직접 처리
- 그 외 `/...`는 Codex 슬래시 명령으로 전달
- `/codex /...` 형식으로 Codex 전달을 강제할 수 있음

## 6) Single 모드 응답 이해
single 모드는 Developer/Reviewer 반복 루프(최대 3회)로 동작한다.

응답 끝에 아래 요약이 붙는다.
```text
[single-review] rounds=2/3, result=approved
```

- `approved`: 리뷰 승인됨
- `max_rounds_reached`: 최대 라운드 도달로 종료됨

## 7) 실행 중 생성 파일
### 세션
- 경로: `~/.codex-orchestrator/sessions/`
- 파일: `{chatId}-{userId}.json`

### trace 로그
- 경로: `~/.codex-orchestrator/traces/`
- 파일: `{yyyy-mm-dd}.jsonl`
- 요청/응답/상태/지연시간 저장
- 민감정보(`token`, `api_key`, `authorization`) 마스킹 저장

## 8) 문제 해결
### bot이 응답하지 않을 때
1. runner 프로세스가 떠 있는지 확인
2. `TELEGRAM_BOT_TOKEN` 값 확인
3. polling 충돌 방지를 위해 webhook 삭제 확인
4. bot과 채팅을 실제로 시작했는지 확인 (`/start` 먼저 입력)

### `/status`에서 `codex_mcp: unknown`
- `CODEX_MCP_STATUS_CMD` 실행 실패 또는 상태 조회 예외 상태
- 상태 조회 명령을 단독으로 먼저 실행해 출력 형식을 점검

### `/status`에서 `codex_mcp: running=false`가 나올 때
1. 실제 프로세스 확인:
   - `ps -eo pid,etimes,args | rg 'codex mcp-server'`
2. 프로세스가 보이지 않으면 orchestrator 기준으로는 실행 중이 아님
3. 프로세스가 보이는데도 false면 `CODEX_MCP_STATUS_CMD`를 명시해 강제 상태 조회 사용

### 응답에 `You are Developer Agent...` 프롬프트가 반복될 때
1. `CODEX_ALLOW_ECHO_EXECUTOR=true`가 켜져 있는지 확인
2. 꺼져 있어야 정상 경로(MCP client + codex tool 호출)로 실행됨
3. `CODEX_MCP_COMMAND`, `CODEX_MCP_ARGS`가 정상인지 확인
4. `/new`로 세션 초기화 후 다시 요청

### 그룹 채팅에서 메시지를 못 받을 때
- BotFather `/setprivacy`를 `Disable`로 변경

## 9) 최소 점검 시나리오
1. `/start`
2. `/status`
3. `file 에 textbox를 추가해`
4. `/status` (last_run, single_review 변화 확인)
5. `/new`
6. `/status` (세션 초기화 확인)
