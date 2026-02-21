# Codex Orchestrator — Design Doc (Personal / Single User / TypeScript)

## 1. Goals

### 1.1 Primary goal
- Run **Codex CLI via MCP Server** continuously and allow controlling it remotely via **Telegram**.
- Support **session continuity** by persisting `threadId` to local files (no DB).

### 1.2 Non-goals (initial)
- Multi-user / multi-tenant auth system
- Distributed deployment / multi-instance HA
- Complex queueing system (Redis, RabbitMQ)
- Web UI

---

## 2. Scope by Roadmap

### v0.1 (MVP)
- Telegram commands:
  - `/run <instruction...>`: start a new session (MCP `codex`)
  - `/reply <instruction...>`: continue session (MCP `codex-reply`)
- Persist `threadId` to file: `.state/session.json`
- Store latest execution output to `.state/logs/latest.log`
- Return summary to Telegram

### v0.2 (Hardening)
- Commands:
  - `/status`: show last job status
  - `/tail [n]`: show last N lines of log
- Add:
  - **single-run lock** to prevent session corruption
  - **atomic writes** for state files
  - timeouts & failure handling
  - structured `latest.json` job metadata

### v0.3+ (Optional)
- Multi-project session separation (e.g., `.state/sessions/<project>.json`)
- Agents SDK workflows (Dev/Reviewer handoff)
- Optional HTTP API (non-Telegram)

---

## 3. System Overview

### 3.1 High-level architecture
```
Telegram Bot
  ↓ (Webhook / Polling)
Orchestrator App (TypeScript)
  ├─ Command Router (run/reply/status/tail)
  ├─ Session Store (file)
  ├─ Job Store (file)
  ├─ Log Store (file)
  ├─ Lock Manager (file)
  └─ MCP Client → Codex MCP Server → codex-cli (workspace)
```

### 3.2 Key invariant
- At any moment, there must be **at most one active execution** that can mutate `threadId` or workspace.
- `threadId` must be **updated atomically** to avoid partial writes on crash.

---

## 4. Interfaces & Responsibilities

### 4.1 Telegram Command Router
Responsibilities:
- Parse Telegram messages
- Map to handlers
- Post responses (including partial/summary)

Commands:
- `/run <instruction...>`
- `/reply <instruction...>`
- `/status`
- `/tail [n]`

### 4.2 Session Store (File-based)
Responsibilities:
- Load/save current session state (`threadId`, execution policy, cwd)

File:
- `.state/session.json`

Schema:
```json
{
  "threadId": "thr_...",
  "cwd": "/workspaces/default",
  "sandbox": "workspace-write",
  "approvalPolicy": "on-request",
  "updatedAt": "2026-02-21T21:00:00+09:00"
}
```

### 4.3 Job Store (Latest only)
Responsibilities:
- Keep only the most recent job metadata (simple personal mode)

Files:
- `.state/jobs/latest.json`
- `.state/logs/latest.log`

Schema (`latest.json`):
```json
{
  "jobId": "job_YYYYMMDD_HHMMSS",
  "status": "queued|running|succeeded|failed",
  "startedAt": "ISO8601",
  "endedAt": "ISO8601|null",
  "threadId": "thr_...",
  "instruction": "string",
  "logPath": ".state/logs/latest.log",
  "error": "string|null"
}
```

### 4.4 Lock Manager (Single execution)
Responsibilities:
- Prevent concurrent `/run` and `/reply`
- Ensure session/threadId does not get overwritten by interleaving requests

File:
- `.state/lock`

Rules:
- Acquire lock before calling MCP
- Release lock in `finally`

Lock format (example):
```json
{
  "pid": 12345,
  "jobId": "job_...",
  "acquiredAt": "ISO8601"
}
```

---

## 5. Execution Flow

### 5.1 /run
1. Acquire lock
2. Create new `jobId`
3. Write `latest.json` status=`running` (atomic)
4. Call MCP tool: `codex` with `{ prompt: instruction, ...policy }`
5. Receive result:
   - Extract `threadId` from structured response
6. Save:
   - `.state/session.json` with new `threadId` (atomic)
   - `.state/logs/latest.log` (overwrite or rotate)
   - `.state/jobs/latest.json` status=`succeeded`
7. Send summary to Telegram
8. Release lock

### 5.2 /reply
Same as `/run`, but:
- Load `.state/session.json` first
- If missing `threadId`, return “No active session. Use /run first.”
- Call MCP tool: `codex-reply` with `{ threadId, prompt: instruction }`

### 5.3 /status
- Read `.state/jobs/latest.json`
- Format output (jobId, status, timestamps, short error if any)

### 5.4 /tail [n]
- Read last N lines from `.state/logs/latest.log`
- Send as Telegram message (truncate if too long)

---

## 6. Policy Defaults (Personal safe-ish defaults)

Config (env or config file):
- `cwd`: `/workspaces/default` (or user provided)
- `sandbox`: `workspace-write`
- `approvalPolicy`: `on-request`
- `timeoutMs`: e.g. 10 minutes

Notes:
- Even for personal use, `danger-full-access` should remain opt-in.

---

## 7. File Layout

```
codex-orchestrator/
  apps/orchestrator/
    src/
      index.ts
      config/
        env.ts
      telegram/
        bot.ts
        commands.ts
      mcp/
        client.ts
        types.ts
      state/
        sessionStore.ts
        jobStore.ts
        logStore.ts
        lock.ts
        atomicWrite.ts
      utils/
        time.ts
        text.ts
    package.json
    tsconfig.json
  docs/
    design.md
```

---

## 8. Implementation Details (TypeScript)

### 8.1 Atomic write pattern
- Write to `path.tmp`, `fsync`, then `rename` to target
- This guarantees target file is either old or new, never partial

Pseudo:
```ts
async function atomicWriteJson(path: string, data: unknown) {
  const tmp = `${path}.tmp`;
  await fs.writeFile(tmp, JSON.stringify(data, null, 2), "utf8");
  await fs.rename(tmp, path);
}
```

### 8.2 Lock acquisition
- Create lock file with `wx` (exclusive)
- If exists: refuse with “Already running job <jobId>”

Pseudo:
```ts
async function acquireLock(lockPath: string, info: LockInfo) {
  const fd = await fs.open(lockPath, "wx"); // fails if exists
  await fd.writeFile(JSON.stringify(info));
  await fd.close();
}

async function releaseLock(lockPath: string) {
  await fs.unlink(lockPath).catch(() => {});
}
```

### 8.3 Output size constraints (Telegram)
- Telegram has message size limits
- Always truncate logs and provide:
  - summary + hint to use `/tail`

---

## 9. Error Handling & Recovery

### 9.1 MCP call fails
- Set job status to `failed` with error message
- Keep previous `session.json` intact (do not overwrite threadId)
- Append error to latest.log

### 9.2 App restarts mid-run
- Lock file may remain
- Provide `/status` showing last state
- Manual recovery approach:
  - If lock older than X minutes, allow “stale lock override” (optional v0.2)

---

## 10. Testing Strategy (Minimal)

- Unit tests:
  - atomicWriteJson
  - lock acquire/release
  - sessionStore load/save
- Integration test (optional):
  - mock MCP client
  - simulate run/reply flows

---

## 11. Security Notes (Personal mode)

- Never store secrets (Telegram token) in `.state/`
- Ensure `.state/` permissions are restricted
- Keep sandbox mode conservative by default

---

## 12. Open Questions (Future)
- Should we rotate logs per job (`logs/<jobId>.log`) instead of `latest.log`?
- Should we support multiple named sessions (`default`, `projectA`)?
- Add optional HTTP API for non-Telegram control?
