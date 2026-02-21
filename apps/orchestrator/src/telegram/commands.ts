import type { McpClient } from "../mcp/types.js";
import type { JobRecord, JobStore } from "../state/jobStore.js";
import { AlreadyLockedError } from "../state/lock.js";
import type { LockManager, LockInfo } from "../state/lock.js";
import type { LogStore } from "../state/logStore.js";
import type { SessionState, SessionStore } from "../state/sessionStore.js";
import { createJobId, nowIso } from "../utils/time.js";
import { clamp, toErrorMessage, truncateForTelegram } from "../utils/text.js";

export interface CommandRouterOptions {
  mcpClient: McpClient;
  sessionStore: SessionStore;
  jobStore: JobStore;
  logStore: LogStore;
  lockManager: LockManager;
  policy: {
    cwd: string;
    sandbox: string;
    approvalPolicy: string;
    timeoutMs: number;
  };
  logPath: string;
  tailDefaultLines: number;
  tailMaxLines: number;
}

type RunMode = "run" | "reply";

export class TelegramCommandRouter {
  private readonly mcpClient: McpClient;
  private readonly sessionStore: SessionStore;
  private readonly jobStore: JobStore;
  private readonly logStore: LogStore;
  private readonly lockManager: LockManager;
  private readonly policy: {
    cwd: string;
    sandbox: string;
    approvalPolicy: string;
    timeoutMs: number;
  };
  private readonly logPath: string;
  private readonly tailDefaultLines: number;
  private readonly tailMaxLines: number;

  constructor(options: CommandRouterOptions) {
    this.mcpClient = options.mcpClient;
    this.sessionStore = options.sessionStore;
    this.jobStore = options.jobStore;
    this.logStore = options.logStore;
    this.lockManager = options.lockManager;
    this.policy = options.policy;
    this.logPath = options.logPath;
    this.tailDefaultLines = options.tailDefaultLines;
    this.tailMaxLines = options.tailMaxLines;
  }

  async handle(text: string): Promise<string> {
    const trimmed = text.trim();
    if (!trimmed.startsWith("/")) {
      return this.helpMessage();
    }

    const [commandToken] = trimmed.split(/\s+/u);
    if (!commandToken) {
      return this.helpMessage();
    }
    const instruction = trimmed.slice(commandToken.length).trim();

    switch (commandToken) {
      case "/run":
        return this.handleRun(instruction);
      case "/reply":
        return this.handleReply(instruction);
      case "/status":
        return this.handleStatus();
      case "/tail":
        return this.handleTail(instruction);
      default:
        return this.helpMessage();
    }
  }

  private async handleRun(instruction: string): Promise<string> {
    if (!instruction) {
      return "Usage: /run <instruction>";
    }

    return this.executeJob("run", instruction, null);
  }

  private async handleReply(instruction: string): Promise<string> {
    if (!instruction) {
      return "Usage: /reply <instruction>";
    }

    const session = await this.sessionStore.loadSession();
    if (!session) {
      return "No active session. Use /run first.";
    }

    return this.executeJob("reply", instruction, session.threadId);
  }

  private async executeJob(mode: RunMode, instruction: string, threadId: string | null): Promise<string> {
    const jobId = createJobId();
    const lockInfo: LockInfo = {
      pid: process.pid,
      jobId,
      acquiredAt: nowIso()
    };

    try {
      await this.lockManager.acquireLock(lockInfo);
    } catch (error) {
      if (error instanceof AlreadyLockedError) {
        const runningJobId = error.lockInfo?.jobId ?? "unknown";
        return `Already running job ${runningJobId}`;
      }
      throw error;
    }

    const runningJob: JobRecord = {
      jobId,
      status: "running",
      startedAt: nowIso(),
      endedAt: null,
      threadId,
      instruction,
      logPath: this.logPath,
      error: null
    };

    await this.jobStore.saveLatestJob(runningJob);

    try {
      const result =
        mode === "run"
          ? await this.mcpClient.run({ prompt: instruction, policy: this.policy })
          : await this.mcpClient.reply({ prompt: instruction, threadId: threadId ?? "", policy: this.policy });

      const resolvedThreadId = result.threadId ?? threadId;
      if (!resolvedThreadId) {
        throw new Error("MCP response does not contain threadId");
      }

      const session: SessionState = {
        threadId: resolvedThreadId,
        cwd: this.policy.cwd,
        sandbox: this.policy.sandbox,
        approvalPolicy: this.policy.approvalPolicy,
        updatedAt: nowIso()
      };

      await this.sessionStore.saveSession(session);
      await this.logStore.writeLatestLog(buildLogContent(jobId, mode, instruction, result.summary, result.rawOutput));

      const succeededJob: JobRecord = {
        ...runningJob,
        status: "succeeded",
        endedAt: nowIso(),
        threadId: resolvedThreadId,
        error: null
      };
      await this.jobStore.saveLatestJob(succeededJob);

      return truncateForTelegram(
        [
          `${mode === "run" ? "/run" : "/reply"} succeeded`,
          `jobId: ${jobId}`,
          `threadId: ${resolvedThreadId}`,
          `summary: ${singleLine(result.summary)}`,
          "Use /tail to inspect full output."
        ].join("\n")
      );
    } catch (error) {
      const message = toErrorMessage(error);

      await this.logStore.appendLatestLog(`\n[${nowIso()}] ERROR ${message}\n`);

      const failedJob: JobRecord = {
        ...runningJob,
        status: "failed",
        endedAt: nowIso(),
        error: message
      };
      await this.jobStore.saveLatestJob(failedJob);

      return truncateForTelegram(
        [
          `${mode === "run" ? "/run" : "/reply"} failed`,
          `jobId: ${jobId}`,
          `error: ${message}`,
          "Use /status and /tail for details."
        ].join("\n")
      );
    } finally {
      await this.lockManager.releaseLock();
    }
  }

  private async handleStatus(): Promise<string> {
    const latest = await this.jobStore.loadLatestJob();
    if (!latest) {
      return "No job history yet.";
    }

    return truncateForTelegram(
      [
        `jobId: ${latest.jobId}`,
        `status: ${latest.status}`,
        `startedAt: ${latest.startedAt}`,
        `endedAt: ${latest.endedAt ?? "-"}`,
        `threadId: ${latest.threadId ?? "-"}`,
        `error: ${latest.error ?? "-"}`
      ].join("\n")
    );
  }

  private async handleTail(argument: string): Promise<string> {
    const rawLineCount = argument.trim();
    const parsed = rawLineCount ? Number.parseInt(rawLineCount, 10) : this.tailDefaultLines;
    const safeLineCount = clamp(Number.isFinite(parsed) ? parsed : this.tailDefaultLines, 1, this.tailMaxLines);

    const content = await this.logStore.tailLatestLog(safeLineCount);
    if (!content) {
      return "No log available yet.";
    }

    return truncateForTelegram(content);
  }

  private helpMessage(): string {
    return [
      "Supported commands:",
      "/run <instruction>",
      "/reply <instruction>",
      "/status",
      "/tail [n]"
    ].join("\n");
  }
}

function buildLogContent(jobId: string, mode: RunMode, instruction: string, summary: string, rawOutput: string): string {
  return [
    `[${nowIso()}] jobId=${jobId}`,
    `mode=${mode}`,
    `instruction=${instruction}`,
    `summary=${singleLine(summary)}`,
    "",
    rawOutput
  ].join("\n");
}

function singleLine(value: string): string {
  return value.replace(/\s+/gu, " ").trim().slice(0, 300);
}
