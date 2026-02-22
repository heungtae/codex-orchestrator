# Single 모드 우선 개발 계획

## 문제 정의
- 목적: Telegram 입력을 Codex에 연결하는 Python 오케스트레이터에서 `single` 모드(Developer/Reviewer 반복 루프)를 먼저 완성해, 실사용 가능한 최소 기능을 빠르게 확보한다.
- 범위: `/mode single`, `/new`, `/status`, 일반 텍스트 입력, 세션 파일 저장(`~/.codex-orchestrator/sessions/`), trace 저장(`~/.codex-orchestrator/traces/`), Codex MCP 상태 조회를 포함한다.
- 성공 기준:
- 단일 채팅에서 일반 문장 입력이 Codex로 전달되고 Developer/Reviewer 루프가 동작한다.
- 리뷰 결과가 `approved` 또는 `max_rounds_reached`로 종료되며 결과가 Telegram 응답에 포함된다.
- `/status`에 `mode`, 최근 실행 상태, single 리뷰 라운드, Codex MCP 상태가 표시된다.
- `/new` 실행 시 세션 히스토리가 초기화된다.
- 세션/trace 파일이 규칙 경로에 저장되고 재기동 후에도 세션 복원이 가능하다.

## 요구사항 구조화
- 기능 요구사항:
- `FR-01` Command Router는 예약 명령(`/mode`, `/new`, `/status`)만 내부 처리하고 나머지 텍스트/슬래시는 Codex로 전달한다.
- `FR-02` Single Workflow는 `Developer -> Reviewer -> (필요 시) Developer` 루프를 최대 `max_review_rounds=3`로 수행한다.
- `FR-03` Reviewer 결과는 `approved | needs_changes | max_rounds_reached`로 표준화한다.
- `FR-04` Session Manager는 `~/.codex-orchestrator/sessions/{chatId}-{userId}.json`에 원자적 저장한다.
- `FR-05` Trace Logger는 `~/.codex-orchestrator/traces/{yyyy-mm-dd}.jsonl`에 `run_id`, `review_round`, `review_result`를 기록한다.
- `FR-06` `/status`는 Codex MCP `get_status()` 결과를 포함해 응답한다.
- `FR-07` `/new`는 세션 상태와 history를 즉시 초기화한다.
- 비기능 요구사항:
- `NFR-01` 동시성 안전: 동일 `chatId:userId`는 mutex로 직렬 처리한다.
- `NFR-02` 장애 허용: trace 저장 실패가 있어도 사용자 응답 플로우는 계속 진행한다.
- `NFR-03` 관측성: 모든 실행에 `run_id`를 부여하고 입력/출력/에러를 추적 가능하게 한다.
- `NFR-04` 보안: trace 저장 전 민감정보(`token`, `api_key`, `authorization`)를 마스킹한다.
- 우선순위:
- `P0` Single 루프 실행, `/status` MCP 포함, `/new`, 세션/trace 파일 저장.
- `P1` 에러 메시지 품질 개선, TTL 정리 배치, 운영성 튜닝.
- `P2` Multi-agent 확장 및 handoff 고도화.

## 제약 조건
- 일정/리소스:
- single 모드 MVP를 먼저 완성하고 multi 모드는 후속 단계로 분리한다.
- 구현/테스트/문서화를 한 사이클에서 끝내기 위해 범위를 P0 중심으로 제한한다.
- 기술 스택/환경:
- Python 기반 구현을 사용한다.
- 세션/trace는 로컬 파일 저장소(`~/.codex-orchestrator/...`)를 사용한다.
- Telegram Bot API + MCP Python SDK + Codex MCP 연동을 전제로 한다.
- 기타:
- Telegram 메시지 길이 제한(4096자) 대응이 필요하다.
- Codex MCP 상태 조회 실패 시 `/status`는 `codex_mcp: unknown`으로 degrade 한다.

## 아키텍처/설계 방향
- 핵심 설계:
- `orchestrator.py`에서 입력 유형을 판별하고 single 모드면 `single_agent_workflow.py`의 리뷰 루프를 실행한다.
- single 루프는 순차형 상태 머신으로 구현한다: `draft -> review -> revise -> review -> ... -> finalize`.
- 세션은 파일 JSON으로 유지하고, trace는 JSONL append 전용으로 저장한다.
- `/status`는 Session 상태 + Codex MCP 상태를 합쳐 단일 응답을 반환한다.
- 대안 및 trade-off:
- 대안 A: Single 모드를 진짜 단일 agent로 구성하면 구현은 단순하지만 품질 게이트(리뷰)가 약하다.
- 대안 B: single 내부 2-agent 루프는 구현 복잡도가 증가하지만 품질/안전성이 높고 현재 요구사항에 정확히 부합한다.
- 선택: 대안 B 채택.
- 리스크:
- 리뷰 루프 장기화로 인한 응답 지연/비용 증가 가능성이 있다.
- Reviewer 피드백의 품질이 낮으면 반복만 늘어날 수 있다.
- 파일 기반 저장소는 다중 프로세스/다중 인스턴스 확장에 약하다.

## 작업 계획
1. 기반 스캐폴딩 정리: `src/bot`, `src/core`, `src/workflows`, `src/integrations` 생성 및 공통 설정/타입 정의.
2. `session_manager.py` 구현: 경로 생성, 파일 로드/저장, 원자적 쓰기, chat-user mutex, `/new` 초기화.
3. `trace_logger.py` 구현: JSONL append, 필드 표준화(`run_id`, `review_round`, `review_result`), 민감정보 마스킹.
4. `codex_mcp.py` 구현: MCP 수명주기, `get_status()` 제공, 실패 시 상태 degrade 처리.
5. `command_router.py` 구현: 예약 명령 처리 + 일반 텍스트/슬래시 Codex 전달 규칙 적용.
6. `single_agent_workflow.py` 구현: Developer/Reviewer 반복 루프, `max_review_rounds` 제한, 종료 상태 표준화.
7. `orchestrator.py` 구현: 실행 제어, run lock, `/status` 응답 구성, trace 연동, Telegram 응답 포맷팅.
8. 테스트 작성: Router 단위 테스트, Single 루프 상태 전이 테스트, 세션/trace I/O 테스트, `/status` 통합 테스트.
9. 수동 E2E 검증: 일반 문장 입력으로 single 루프 실행, `/new` 초기화, `/status` MCP 상태 확인.
10. 완료 검토: P0 체크리스트 통과 확인 후 multi 모드 구현 계획으로 전환.
