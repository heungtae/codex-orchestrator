# Telegram + Codex MCP 워크플로우 설계안 (Python)

## 1) 목표
- Telegram Bot 명령/메시지를 받아 Codex MCP tool 기반 워크플로우를 실행한다.
- 1단계: `single` 모드에서 `Planner -> Developer -> Reviewer` 워크플로우를 구현한다.
- 2단계: `Expand to a multi-agent workflow`를 구현한다.
- 동일한 Telegram 인터페이스에서 단일/다중 모드를 전환할 수 있게 한다.

## 2) 기준 문서
- Codex Agents SDK 가이드:
  - https://developers.openai.com/codex/guides/agents-sdk

## 3) 아키텍처 개요
```text
[Telegram User]
   -> [Telegram Bot Adapter]
   -> [Command Router]
   -> [Session Manager] <-> [File Store (JSON)]
   -> [Trace Logger] <-> [Trace File Store (JSONL)]
   -> [Agent Orchestrator]
        -> [Single-Agent Runner] OR [Multi-Agent Runner]
        -> [MCP Tool Client]
        -> [Codex MCP Server(s)]
   -> [Response Formatter]
   -> [Telegram Bot API]
```

## 4) 모듈 설계 (Python)
### `src/bot/telegram_adapter.py`
- Telegram 업데이트 수신 (Webhook 권장, 개발 시 Polling 가능).
- `chat_id`, `user_id`, message text 추출.
- Telegram 응답 길이 제한(4096 chars) 분할 전송 지원.

### `src/core/command_router.py`
- 명령 파싱:
  - Bot 예약 명령: `/mode single`, `/mode multi`, `/new`, `/status`, `/profile ...`
  - Codex 슬래시 명령: `/...` (예약 명령 제외 시 그대로 Codex로 전달)
  - 일반 텍스트(기본 경로: Codex로 즉시 전달)
- 라우팅 우선순위:
  - `bot_command` -> 예약 명령 직접 처리
  - `codex_slash` -> Codex로 슬래시 원문 전달
  - `text` -> Codex로 자연어 요청 전달
- 파싱 결과를 `BotCommand` DTO로 표준화.

### `src/core/session_manager.py`
- 키: `tg:{chatId}:{userId}`.
- 파일 경로: `~/.codex-orchestrator/sessions/{chatId}-{userId}.json`
- 저장 데이터:
  - 현재 모드(single|multi), 기본값은 `single`
  - 대화 history
  - 현재 실행 중 플래그(동시 실행 방지)
  - 최근 에러/메타데이터
- 구현 규칙:
  - 원자적 저장(임시 파일 쓰기 후 rename)
  - 프로세스 내 `chatId:userId` 단위 mutex로 동시 쓰기 방지
  - `~/.codex-orchestrator/sessions/` 디렉터리 자동 생성
  - Python에서는 `~`를 직접 경로로 쓰지 않고 `Path.home()` 기반으로 절대 경로 변환

### `src/workflows/agent_factory.py`
- single 모드용 3-stage 체인(Planner/Developer/Reviewer)과 multi 모드 에이전트 트리(리드/서브) 생성.
- 공통 Codex executor 인스턴스 재사용.
- 모델/프롬프트/가드레일을 중앙 관리.

### `src/workflows/single_agent_workflow.py`
- `Planner Agent` + `Developer Agent` + `Reviewer Agent` 단계를 single 모드 내부에서 실행.
- 한 번의 사용자 요청에서:
  - Planner가 구현 계획(handoff) 생성
  - Developer가 구현/수정 수행
  - Reviewer가 결과 검토(버그, 누락, 리스크, 테스트 관점)
  - 수정 필요 시 Reviewer 피드백을 Developer에 재투입
  - 승인 또는 최대 반복(`max_review_rounds`, 기본 3회) 도달 시 종료
  - 최종 결과 + 리뷰 요약 텍스트 반환

### `src/workflows/multi_agent_workflow.py`
- 권장 역할:
  - `Triage/Lead Agent`: 요청 분류, 작업 분해, handoff 결정
  - `Engineer Agent`: 구현/수정 중심
  - `Reviewer Agent`: 검증/리스크 점검 중심
- handoff 기반으로 하위 에이전트 위임 후 리드가 최종 응답 통합.

### `src/integrations/codex_mcp.py`
- Codex MCP 서버 연결/수명주기 관리.
- 서버 시작/종료, 타임아웃, 재시도 처리.
- 워크스페이스 경로 및 허용 명령 정책 적용.
- 상태 조회 API 제공:
  - `get_status(): {"running": bool, "ready": bool, "pid": int | None, "uptime_sec": int | None, "last_error": str | None}`

### `src/core/orchestrator.py`
- Telegram 입력 -> 세션 조회 -> 모드별 실행 -> 결과 저장 -> 응답 전송.
- chat 단위 직렬화 큐(동일 chat 동시 요청 충돌 방지).
- 실행 단위(`run_id`)로 요청 입력/응답 출력을 `TraceLogger`에 기록.
- `/status` 처리 시 세션 상태 + Codex MCP 상태를 함께 조회해 단일 응답으로 반환.

### `src/core/trace_logger.py`
- trace 파일 경로: `~/.codex-orchestrator/traces/{yyyy-mm-dd}.jsonl`
- 저장 대상:
  - Telegram 입력 원문
  - 워크플로우 최종 출력 텍스트
  - 실행 메타데이터(`run_id`, `session_id`, `mode`, `latency_ms`, `status`, `error`)
- 구현 규칙:
  - JSONL append 전용(1 run = 1 line)
  - 민감정보 마스킹 후 저장(`token`, `api_key`, `authorization`)
  - Python에서는 `Path.home()`로 `~`를 절대 경로로 변환

## 5) 명령 계약 (v1)
- `/start`: 사용법 안내
- `/mode single`: single 모드(planner/developer/reviewer) 전환
- `/mode multi`: 다중 에이전트 모드 전환
- `/new`: 해당 세션 대화/상태 초기화
- `/status`: 현재 모드/최근 실행 상태 + Codex MCP 상태 출력
- `/profile list`: 사용 가능한 profile 목록 출력
- `/profile <name>`: 실행 profile 전환(`model`, `working_directory`)
- `텍스트`: 현재 모드의 워크플로우로 실행 (기본적으로 Codex에 전달)
- 예약 명령(`/mode`, `/new`, `/status`, `/profile`)이 아닌 `/...` 입력은 Codex 슬래시 명령으로 전달
- 예약 명령이 아닌 일반 문장 입력은 항상 Codex로 전달
- `/new` 전까지는 같은 세션 history를 유지하며 연속 명령으로 처리
- 모드를 설정하지 않은 신규 세션의 기본 모드는 `single`

### `/status` 응답 포맷 (예시)
```text
mode: single
profile: bridge, model=gpt-5, working_directory=/home/user/develop/bridge-project
last_run: ok (4200ms)
single_review: rounds=2/3, result=approved
codex_mcp: running=true, ready=true, pid=12345, uptime=532s
last_error: -
```

## 6) 워크플로우 상세
### A. Single-mode (planner/developer/reviewer)
1. 사용자가 메시지 입력
2. Router가 입력 유형 판별 (예약 명령 | Codex 슬래시 명령 | 일반 텍스트)
3. SessionManager가 현재 모드 확인 (신규 세션이면 `single`로 초기화)
4. Planner Agent가 요청 기반 구현 계획(handoff) 생성
5. Developer Agent가 구현/수정 초안을 생성
6. Reviewer Agent가 초안을 검토하고 승인 여부/수정 피드백 생성
7. 미승인이고 반복 한도 미만이면 Reviewer 피드백을 Developer에 전달해 5~6 반복
8. 승인 또는 반복 한도 도달 시 결과를 요약해 Telegram으로 반환
9. history/state 저장

### B. Multi-agent
1. 사용자 입력 수신
2. Lead Agent가 요청을 분해
3. handoff로 Engineer/Reviewer Agent에 위임
4. 하위 에이전트 결과를 Lead Agent가 합성
5. 최종 응답 반환 및 세션 저장

### C. Codex 슬래시 명령 처리
1. 입력이 `/...` 형태면 Router가 예약 명령 여부를 먼저 확인
2. 예약 명령이 아니면 원문 그대로 현재 모드 워크플로우에 전달
3. 실행 결과를 일반 요청과 동일하게 응답/trace 저장

### D. 일반 텍스트 처리 (기본 동작)
1. 입력이 예약 명령과 슬래시 명령이 아니면 `text`로 분류
2. `text` 입력은 현재 모드(single/multi)의 Codex 실행 경로로 즉시 전달
3. Session history에 누적되어 다음 입력에도 같은 컨텍스트를 사용
4. 예시: `file 에 textbox를 추가해` -> Codex 실행 -> 결과 응답

## 7) 상태/저장소 설계
### 최소 스키마
- `session_id` (chat+user 기반)
- `mode` (`single` | `multi`)
- `profile_name` (예: `default`, `bridge`)
- `profile_model` (선택)
- `profile_working_directory` (선택)
- `history_json` (대화 히스토리 아이템)
- `run_lock` (boolean)
- `last_review_round` (number, single 모드용)
- `last_review_result` (`approved` | `needs_changes` | `max_rounds_reached`)
- `updated_at`

### 파일 저장소 설계
- 루트 디렉터리: `~/.codex-orchestrator/sessions/`
- 파일명: `{chatId}-{userId}.json`
- 포맷: 단일 세션당 JSON 1파일
- 쓰기 방식: `*.tmp`에 기록 후 `rename`으로 교체(원자성 보장)
- 정리 정책: `updated_at` 기준 TTL 배치 정리(예: 30일)
- 참고: 다중 인스턴스 확장 시 Redis/Postgres로 전환 가능

### trace 로그 저장소 설계
- 루트 디렉터리: `~/.codex-orchestrator/traces/`
- 파일명: `{yyyy-mm-dd}.jsonl`
- 포맷: JSONL (요청 1건당 1라인)
- 로그 필드:
  - `timestamp`
  - `run_id`
  - `session_id`
  - `mode`
  - `review_round` (single 모드 시)
  - `review_result` (single 모드 시)
  - `input_kind` (`bot_command` | `codex_slash` | `text`)
  - `input_text`
  - `output_text`
  - `status` (`ok` | `error`)
  - `latency_ms`
  - `error_message` (실패 시)
- 정리 정책: 파일 기준 TTL 배치 정리(예: 30일)

## 8) 오류/예외 처리
- MCP 서버 시작 실패: 사용자에게 재시도 메시지 + 내부 알림
- 에이전트 timeout: 요청 취소 후 세션 락 해제
- Telegram 전송 실패(429 등): 지수 백오프 재시도
- 세션 파일 읽기/쓰기 실패: 기본값(`single`, 빈 history)으로 복구 후 경고 로그
- trace 파일 쓰기 실패: 서비스 플로우는 계속 진행, 에러 로그만 남김
- `/status`에서 MCP 상태 조회 실패: `codex_mcp: unknown`으로 응답하고 내부 경고 로그 저장
- 지원하지 않는 Codex 슬래시 명령 입력: 에러 메시지와 함께 사용 가능한 슬래시 명령 형식(`/...`) 안내
- single 모드 리뷰 반복 한도 초과: 마지막 Reviewer 피드백과 함께 `max_rounds_reached` 상태로 응답
- 예상치 못한 예외: 사용자에게 간단 오류 메시지, 내부 로그는 상세 저장

## 9) 보안/운영 가이드
- Telegram webhook secret 검증.
- 허용된 사용자/채팅 allowlist(초기 운영 시 권장).
- Codex MCP에 전달되는 경로를 프로젝트 루트로 제한.
- 실행 명령 정책(허용/차단 목록) 분리.
- 민감정보(토큰/API 키) 마스킹 로깅.
- trace 로그 열람 권한을 운영자 계정으로 제한(파일 권한 `700`/`600` 권장).

## 10) 구현 순서 제안
1. `telegram_adapter` + `command_router` + `session_manager` 구현
2. single 모드(planner/developer/reviewer) 워크플로우 연결
3. `/mode`, `/new`, `/status` 명령 완성
4. Multi-agent(handoffs) 추가
5. 관측성(로그/트레이싱) + 재시도/타임아웃 튜닝
6. 부하/장애 테스트 후 운영 배포

## 11) 인터페이스 초안
```python
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol, TypedDict

BotMode = Literal["single", "multi"]


@dataclass
class BotSession:
    session_id: str
    mode: BotMode = "single"
    history: list[Any] = field(default_factory=list)  # conversation items
    run_lock: bool = False
    updated_at: str = ""


class WorkflowResult(TypedDict):
    output_text: str
    next_history: list[Any]


class Workflow(Protocol):
    async def run(self, input_text: str, session: BotSession) -> WorkflowResult: ...


class CodexMcpStatus(TypedDict, total=False):
    running: bool
    ready: bool
    pid: int
    uptime_sec: int
    last_error: str
}
```

## 12) 산출물 기준 (완료 정의)
- Telegram에서 모드 전환 명령이 정상 동작한다.
- 동일 채팅에서 single/multi 각각 1회 이상 성공 실행된다.
- 실행 도중 실패해도 세션 락이 해제된다.
- history가 유지되어 연속 대화가 가능하다.
- 운영 로그에서 요청-응답-에러 추적이 가능하다.
- 일반 문장(슬래시 없음) 입력이 Codex로 전달되어 작업이 수행된다.
- single 모드에서 planner 실행 후 developer/reviewer 루프가 반복되고, 승인 또는 최대 반복 도달 결과가 반환된다.
