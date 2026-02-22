# codex-orchestrator

Telegram Bot에서 Codex 워크플로우(single/multi)를 실행하기 위한 Python 오케스트레이터입니다.

## 주요 기능
- Telegram long polling 기반 요청 처리
- `/mode`, `/profile`, `/cancel` 등 운영 명령 라우팅
- 사용자 허용 목록(`telegram.allowed_users`) 기반 접근 제어
- Codex MCP warmup 및 상태 확인
- 세션/트레이스 파일 저장
- 표준출력 로그 타임스탬프 자동 prefix

## 프로젝트 구조
- `src/core`: 라우팅, 오케스트레이션, 세션, 프로파일, 트레이스
- `src/workflows`: single/multi 워크플로우
- `src/integrations`: Codex executor, MCP 상태 연동
- `src/bot`: Telegram update 파싱, 메시지 분할
- `scripts/telegram_polling_runner.py`: 운영 진입점
- `tests`: `unittest` 테스트

## 요구 사항
- Python 3.11+
- `npx` + `codex mcp-server`를 실행할 수 있는 환경
- Telegram Bot 토큰

## 설치
```bash
python3 -m pip install mcp python-dotenv
```

## 사용자 설정 파일
1. 환경변수 템플릿 준비
```bash
cp .env.example .env
```

2. 사용자 conf 준비
```bash
mkdir -p ~/.codex-orchestrator
cp conf.toml.example ~/.codex-orchestrator/conf.toml
```

3. 최소 필수 설정
- `.env`의 `TELEGRAM_BOT_TOKEN` 값을 실제 토큰으로 변경
- `~/.codex-orchestrator/conf.toml`의 `telegram.allowed_users`를 실제 사용자 ID로 변경
- 필요 시 `conf.toml`의 `codex.*`, `telegram.polling.*`로 런타임 옵션 조정

참고:
- `CODEX_CONF_PATH`를 설정하면 기본 경로(`~/.codex-orchestrator/conf.toml`) 대신 해당 파일을 사용합니다.
- 상대 경로 `working_directory`와 `system_prompt_file`은 conf 파일 위치 기준으로 해석됩니다.

## 실행
```bash
PYTHONPATH=src python3 scripts/telegram_polling_runner.py
```

## 테스트
전체:
```bash
PYTHONPATH=src python3 -m unittest discover -s tests -p 'test_*.py' -q
```

특정 모듈:
```bash
PYTHONPATH=src python3 -m unittest -q tests.test_telegram_polling_runner
```

## Telegram 명령
- `/start`: 명령 안내
- `/mode single|multi`: 모드 전환
- `/new`: 현재 세션 초기화
- `/status`: 실행 상태 확인
- `/cancel`: 실행 중 요청 취소
- `/profile list|<name>`: 프로파일 목록/전환

## 운영 파일
- 세션: `~/.codex-orchestrator/sessions/{chatId}-{userId}.json`
- 트레이스: `~/.codex-orchestrator/traces/{yyyy-mm-dd}.jsonl`

## 추가 문서
- `docs/telegram-integration-runbook.md`: Telegram 연동/운영 절차
- `docs/usage-single-mode.md`: single 모드 중심 사용 가이드
