import path from "node:path";

import { parsePositiveInteger } from "../utils/text.js";

export interface AppConfig {
  telegramToken: string;
  telegramAllowedChatId: number | null;
  pollTimeoutSeconds: number;
  pollIntervalMs: number;
  stateDir: string;
  defaultCwd: string;
  sandbox: string;
  approvalPolicy: string;
  timeoutMs: number;
  mcpRunCommand: string;
  mcpReplyCommand: string;
  staleLockOverrideMs: number;
  tailDefaultLines: number;
  tailMaxLines: number;
}

function required(env: NodeJS.ProcessEnv, key: string): string {
  const value = env[key];
  if (!value) {
    throw new Error(`Missing required environment variable: ${key}`);
  }
  return value;
}

function parseNullableChatId(value: string | undefined): number | null {
  if (!value) {
    return null;
  }

  const parsed = Number.parseInt(value, 10);
  if (!Number.isFinite(parsed)) {
    throw new Error("TELEGRAM_ALLOWED_CHAT_ID must be a valid integer");
  }

  return parsed;
}

function parseNonNegativeInt(value: string | undefined, fallback: number): number {
  if (!value) {
    return fallback;
  }

  const parsed = Number.parseInt(value, 10);
  if (!Number.isFinite(parsed) || parsed < 0) {
    return fallback;
  }

  return parsed;
}

export function loadConfig(env: NodeJS.ProcessEnv = process.env): AppConfig {
  const staleMinutes = parseNonNegativeInt(env.STALE_LOCK_OVERRIDE_MINUTES, 0);

  return {
    telegramToken: required(env, "TELEGRAM_BOT_TOKEN"),
    telegramAllowedChatId: parseNullableChatId(env.TELEGRAM_ALLOWED_CHAT_ID),
    pollTimeoutSeconds: parsePositiveInteger(env.TELEGRAM_POLL_TIMEOUT_SECONDS, 25),
    pollIntervalMs: parsePositiveInteger(env.TELEGRAM_POLL_INTERVAL_MS, 1500),
    stateDir: path.resolve(env.STATE_DIR ?? ".state"),
    defaultCwd: path.resolve(env.CODEX_WORKSPACE_CWD ?? process.cwd()),
    sandbox: env.CODEX_SANDBOX ?? "workspace-write",
    approvalPolicy: env.CODEX_APPROVAL_POLICY ?? "on-request",
    timeoutMs: parsePositiveInteger(env.CODEX_TIMEOUT_MS, 10 * 60 * 1000),
    mcpRunCommand: env.MCP_RUN_COMMAND ?? "codex mcp call codex",
    mcpReplyCommand: env.MCP_REPLY_COMMAND ?? "codex mcp call codex-reply",
    staleLockOverrideMs: staleMinutes * 60 * 1000,
    tailDefaultLines: parsePositiveInteger(env.TAIL_DEFAULT_LINES, 80),
    tailMaxLines: parsePositiveInteger(env.TAIL_MAX_LINES, 300)
  };
}
