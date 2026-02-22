# Telegram Integration Runbook

이 문서는 `codex-orchestrator`를 Telegram long polling으로 연동하고 운영할 때 필요한 절차만 다룹니다.

## 1) 범위
- Telegram Bot 생성
- 로컬 실행 환경 설정
- polling runner 실행/검증
- 운영 중 점검 및 장애 대응

제외 범위:
- 내부 아키텍처 상세 설계
- 기능 설계 문서 수준의 워크플로우 설명

## 2) 사전 준비
- Python 3.11+
- Telegram 계정
- `npx` 및 `codex mcp-server` 실행 가능 환경
- 레포 루트 경로 접근

의존성 설치:
```bash
python3 -m pip install mcp python-dotenv
```

## 3) Telegram Bot 생성 (BotFather)
1. Telegram에서 `@BotFather`를 연다.
2. `/newbot` 실행 후 bot 이름/username을 등록한다.
3. 발급된 Bot Token을 보관한다. (예: `123456:ABC...`)

선택 설정:
- 그룹 채팅에서 일반 메시지를 받으려면 `/setprivacy`를 `Disable`로 설정한다.

## 4) 런타임 설정
### 4.1 환경 변수
```bash
export TELEGRAM_BOT_TOKEN='발급받은_토큰'
# 선택: 기본값 ~/.codex-orchestrator/conf.toml
# export CODEX_CONF_PATH="$HOME/.codex-orchestrator/conf.toml"
```

### 4.2 conf 파일 준비
```bash
mkdir -p ~/.codex-orchestrator
cp conf.toml.example ~/.codex-orchestrator/conf.toml
```

참고:
- conf 파일이 없으면 runner 최초 실행 시 기본 템플릿을 자동 생성한다.

### 4.3 최소 conf 예시
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
# mcp_status_cmd = "bash -lc \"echo running=true,ready=true,pid=12345,uptime_sec=30\""
mcp_auto_detect_process = false

[profile]
default = "default"

[profiles.default]
model = "gpt-5"
working_directory = "~/develop/your-project"
```

운영 메모:
- `telegram.allowed_users`를 설정하면 목록 외 사용자는 `Unauthorized`로 차단된다.
- `codex.mcp_direct_status=true`일 때는 `mcp_status_cmd`, `mcp_auto_detect_process`가 사용되지 않는다.
- 에이전트별 프롬프트/모델 튜닝은 필요 시 `agents.*` 키로 별도 설정한다.

## 5) 실행
```bash
PYTHONPATH=src python3 scripts/telegram_polling_runner.py
```

실행 중단:
- 포그라운드 실행 기준 `Ctrl+C`

## 6) Telegram에서 운영 점검
초기 점검 순서:
1. `/start`
2. `/status`
3. 일반 요청 1건 전송 (예: `file 에 textbox를 추가해`)
4. `/status` 재확인
5. `/cancel` (동작 확인 필요 시)
6. `/new`

주요 명령:
- `/start`: 명령 안내
- `/status`: 모드/프로파일/최근 실행/codex_mcp 상태 확인
- `/profile list|<name>`: 프로파일 조회/전환
- `/mode single|multi`: 실행 모드 전환
- `/cancel`: 현재 세션 실행 취소 요청
- `/new`: 현재 세션 초기화

## 7) 운영 파일 위치
- 세션: `~/.codex-orchestrator/sessions/{chatId}-{userId}.json`
- 트레이스: `~/.codex-orchestrator/traces/{yyyy-mm-dd}.jsonl`

## 8) 장애 대응
### bot 응답 없음
1. runner 프로세스가 실행 중인지 확인
2. `TELEGRAM_BOT_TOKEN` 값 확인
3. polling 시작 시 webhook 삭제가 정상 수행됐는지 로그 확인
4. bot과 실제 채팅을 시작했는지 확인 (`/start`)

### `Unauthorized` 응답
- `telegram.allowed_users`에 본인 `from_user.id`가 포함되어 있는지 확인

### `/status`가 `codex_mcp: unknown`
- `codex.mcp_status_cmd` 사용 시 단독 실행으로 출력 형식(JSON 또는 key=value) 확인
- `mcp_direct_status` 설정값과 충돌 여부 확인

### `/status`가 `codex_mcp: running=false`
1. `codex mcp-server` 실제 프로세스 확인
2. 프로세스가 없으면 MCP 실행 경로(`mcp_command`, `mcp_args`) 점검
3. 프로세스가 있는데 false면 상태 조회 방식(`mcp_direct_status` / `mcp_status_cmd`) 점검

### 에이전트 프롬프트 문구가 그대로 응답됨
1. `codex.allow_echo_executor`가 `false`인지 확인
2. `codex.mcp_command`, `codex.mcp_args` 점검
3. `/new`로 세션 초기화 후 재시도

### 그룹 채팅에서 메시지 미수신
- BotFather `/setprivacy`를 `Disable`로 변경

## 9) 운영 권장 사항
- 토큰/민감정보는 환경 변수로만 관리하고 Git에 커밋하지 않는다.
- 운영 초기에는 `telegram.allowed_users`를 활성화해 접근 범위를 제한한다.
- 장기 운영 시 `tmux`/`systemd` 같은 프로세스 관리 도구 사용을 권장한다.
