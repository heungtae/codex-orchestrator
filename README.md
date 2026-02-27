# codex-orchestrator

Telegram Bot에서 Codex 워크플로우(단일/플랜)를 실행하는 Python 오케스트레이터입니다.

## 주요 기능
- Telegram 장기 폴링으로 요청 처리
- `/mode`, `/profile`, `/cancel` 같은 운영 명령 라우팅
- 단일 모드: 하나의 개발자 에이전트를 즉시 실행
- 플랜 모드: `selector -> planner -> developer -> reviewer` 순으로 실행 (selector가 요청을 분석하여 단일/플랜으로 자동 라우팅; 최대 3회 리뷰 라운드)
- 허용 목록(`telegram.allowed_users`)으로 접근 제어
- Codex MCP 워밍업 및 상태 확인
- 세션/트레이스 파일 지속성
- stdout 로그에 자동 타임스탬프 접두사 추가

## 프로젝트 구조
- `src/core`: 라우팅, 오케스트레이션, 세션, 프로필, 트레이스
- `src/workflows`: 단일/플랜 워크플로우
- `src/integrations`: Codex 실행기 및 MCP 상태 통합
- `src/bot`: Telegram 업데이트 파싱 및 메시지 분할
- `scripts/telegram_polling_runner.py`: 운영 진입점
- `tests`: `unittest` 테스트

## 요구 사항
- Python 3.10 이상
- `npx` 및 `codex mcp-server`를 실행할 수 있는 환경
- Telegram Bot 토큰

## 설치
기본 설치:
```bash
python3 -m pip install codex_orchestrator
```

특정 버전 설치:
```bash
python3 -m pip install "codex_orchestrator==<원하는_버전>"
# 예시: python3 -m pip install "codex_orchestrator==0.1.4"
```

버전 변경(업그레이드/다운그레이드):
```bash
python3 -m pip install --upgrade "codex_orchestrator==<원하는_버전>"
```

Ubuntu/Debian 기반 시스템에서 다음 오류가 발생할 수 있습니다:
`error: externally-managed-environment`

이 오류가 발생하면 설치 옵션 1(사용자 로컬 설치):
```bash
python3 -m pip install --user --break-system-packages codex_orchestrator
```

`--user` 옵션을 사용하면 바이너리가 `~/.local/bin`에 설치되므로 PATH를 업데이트하세요:
```bash
echo 'export PATH="$HOME/.local/bin:$PATH"' >> ~/.bashrc
source ~/.bashrc
hash -r
```

이 오류가 발생하면 설치 옵션 2(가상환경):
```bash
python3 -m venv .venv
source .venv/bin/activate
python3 -m pip install -U pip
python3 -m pip install codex_orchestrator
```

개발 환경에서 로컬 소스를 직접 실행하려면:
```bash
python3 -m pip install mcp python-dotenv
```

## 사용자 구성 파일
1. 환경 변수 템플릿 준비
```bash
cp .env.example .env
```

2. 사용자 구성 준비
```bash
mkdir -p ~/.codex-orchestrator
cp conf.toml.example ~/.codex-orchestrator/conf.toml
```

3. 최소 필수 설정
- `.env`에서 `TELEGRAM_BOT_TOKEN`을 실제 토큰으로 교체
- `~/.codex-orchestrator/conf.toml`에서 `telegram.allowed_users`를 실제 사용자 ID로 교체
- 필요하면 `conf.toml`의 런타임 옵션(`codex.*`, `telegram.polling.*`) 조정

주의:
- `CODEX_CONF_PATH`가 설정되어 있으면 기본 경로(`~/.codex-orchestrator/conf.toml`) 대신 해당 파일을 사용합니다.
- `working_directory`와 `system_prompt_file`의 상대 경로는 구성 파일 위치를 기준으로 해석됩니다.

## 실행
PyPI 설치 후:
```bash
codex-orchestrator
```

`command not found` 오류가 발생하면:
```bash
~/.local/bin/codex-orchestrator
```

로컬 소스에서 실행하려면:
```bash
PYTHONPATH=src python3 scripts/telegram_polling_runner.py
```

## 테스트
전체 테스트:
```bash
PYTHONPATH=src python3 -m unittest discover -s tests -p 'test_*.py' -q
```

단일 모듈 테스트:
```bash
PYTHONPATH=src python3 -m unittest -q tests.test_telegram_polling_runner
```

## Telegram 명령어
- `/start`: 명령어 도움말
- `/mode single|plan`: 모드 전환 (기본: plan)
- `/new`: 현재 세션 초기화
- `/status`: 실행 상태 확인
- `/cancel`: 진행 중인 요청 취소
- `/profile list|<name>`: 프로필 목록/전환

## 운영 파일
- 세션: `~/.codex-orchestrator/sessions/{chatId}-{userId}.json`
- 트레이스: `~/.codex-orchestrator/traces/{yyyy-mm-dd}.jsonl`

## 추가 문서
- `docs/telegram-integration-runbook.md`: Telegram 통합/운영 런북
- `docs/usage-single-mode.md`: 단일 모드 중심 사용 가이드
