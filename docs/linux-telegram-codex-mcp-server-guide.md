# Linux 기준: Telegram 설치부터 Codex MCP Server 실행까지

이 문서는 **Linux 환경에서** 아래 순서를 따라가도록 구성했습니다.
1. Telegram 설치
2. Telegram Bot 생성
3. Node.js 설치
4. Codex CLI 설치 및 로그인
5. Codex MCP Server 실행/등록

## 0) 사전 확인
- OS: Ubuntu/Debian 계열 기준 (다른 배포판은 명령만 바꿔 적용)
- 권한: `sudo` 가능 계정
- 네트워크: 인터넷 연결 필요

---

## 1) Telegram 설치 (Linux)

### Ubuntu/Debian
```bash
sudo apt update
sudo apt install -y telegram-desktop
```

실행:
```bash
telegram-desktop
```

로그인 후 정상 실행만 확인하면 됩니다.

---

## 2) Telegram Bot 만들기 (BotFather)

1. Telegram에서 `@BotFather` 검색
2. `/newbot` 입력
3. 봇 이름, 봇 username 입력
4. 발급된 토큰 저장 (예: `123456:ABC...`)

이 토큰은 나중에 `TELEGRAM_BOT_TOKEN` 환경변수로 사용합니다.

---

## 3) Node.js 설치 (Codex CLI 준비)

권장: `nvm`으로 Node 20+ 설치

```bash
curl -fsSL https://raw.githubusercontent.com/nvm-sh/nvm/v0.40.3/install.sh | bash
source ~/.bashrc
nvm install 22
node -v
npm -v
```

---

## 4) Codex CLI 설치 및 로그인

```bash
npm install -g @openai/codex
codex --version
codex login
```

로그인이 완료되면 Codex CLI 사용 준비가 끝납니다.

---

## 5) Codex MCP Server 실행

Codex CLI에는 MCP Server 실행 명령이 포함되어 있습니다.

### 5-1) 단독 실행(동작 확인)
```bash
codex mcp-server
```

- 이 명령은 stdio 기반 MCP 서버를 실행합니다.
- 터미널이 대기 상태로 유지되는 것이 정상입니다.

### 5-2) Codex CLI에 MCP 서버 등록
```bash
codex mcp add local-codex -- codex mcp-server
codex mcp list
codex mcp get local-codex
```

삭제:
```bash
codex mcp remove local-codex
```

---

## 6) (옵션) 이 저장소 오케스트레이터와 연결

`apps/orchestrator`를 사용할 경우 최소 환경변수:

```bash
export TELEGRAM_BOT_TOKEN="<BotFather 토큰>"
```

실행:
```bash
cd apps/orchestrator
npm install
npm run build
npm run dev
```

참고:
- 오케스트레이터는 `MCP_RUN_COMMAND`, `MCP_REPLY_COMMAND` 환경변수로 MCP 호출 커맨드를 받습니다.
- 기본값/구성은 `apps/orchestrator/README.md`를 확인하세요.

---

## 7) 빠른 문제 해결

### `codex: command not found`
- `npm install -g @openai/codex` 재실행
- shell 재실행 후 `codex --version` 확인

### `codex login` 실패
- 네트워크/방화벽 확인
- 인증 절차 재시도

### Telegram 봇 응답 없음
- Bot token 확인
- 봇에게 먼저 메시지를 보냈는지 확인
- 오케스트레이터 로그 확인: `.state/logs/latest.log`
