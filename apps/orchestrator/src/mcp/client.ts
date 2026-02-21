import { spawn } from "node:child_process";

import type { McpClient, McpExecutionResult, McpReplyRequest, McpRunRequest } from "./types.js";

interface CommandLayout {
  command: string;
  useStdin: boolean;
}

export interface CommandMcpClientOptions {
  runCommand: string;
  replyCommand: string;
}

export class CommandMcpClient implements McpClient {
  private readonly runCommand: string;
  private readonly replyCommand: string;

  constructor(options: CommandMcpClientOptions) {
    this.runCommand = options.runCommand;
    this.replyCommand = options.replyCommand;
  }

  async run(request: McpRunRequest): Promise<McpExecutionResult> {
    return this.executeCommand(this.runCommand, {
      prompt: request.prompt,
      cwd: request.policy.cwd,
      sandbox: request.policy.sandbox,
      approvalPolicy: request.policy.approvalPolicy,
      timeoutMs: request.policy.timeoutMs
    });
  }

  async reply(request: McpReplyRequest): Promise<McpExecutionResult> {
    return this.executeCommand(this.replyCommand, {
      threadId: request.threadId,
      prompt: request.prompt,
      cwd: request.policy.cwd,
      sandbox: request.policy.sandbox,
      approvalPolicy: request.policy.approvalPolicy,
      timeoutMs: request.policy.timeoutMs
    });
  }

  private async executeCommand(commandTemplate: string, payload: Record<string, unknown>): Promise<McpExecutionResult> {
    const payloadJson = JSON.stringify(payload);
    const commandLayout = this.resolveCommand(commandTemplate, payloadJson);
    const timeoutMs = typeof payload.timeoutMs === "number" && payload.timeoutMs > 0 ? payload.timeoutMs : 0;

    return new Promise((resolve, reject) => {
      const processRef = spawn("/bin/bash", ["-lc", commandLayout.command], {
        stdio: "pipe"
      });

      const stdoutChunks: string[] = [];
      const stderrChunks: string[] = [];
      let settled = false;
      let timeoutHandle: NodeJS.Timeout | undefined;

      const settleResolve = (result: McpExecutionResult): void => {
        if (settled) {
          return;
        }
        settled = true;
        if (timeoutHandle) {
          clearTimeout(timeoutHandle);
        }
        resolve(result);
      };

      const settleReject = (error: Error): void => {
        if (settled) {
          return;
        }
        settled = true;
        if (timeoutHandle) {
          clearTimeout(timeoutHandle);
        }
        reject(error);
      };

      processRef.stdout.on("data", (chunk: Buffer) => {
        stdoutChunks.push(chunk.toString("utf8"));
      });

      processRef.stderr.on("data", (chunk: Buffer) => {
        stderrChunks.push(chunk.toString("utf8"));
      });

      processRef.on("error", (error) => {
        settleReject(error);
      });

      if (commandLayout.useStdin) {
        processRef.stdin.write(payloadJson);
      }
      processRef.stdin.end();

      if (timeoutMs > 0) {
        timeoutHandle = setTimeout(() => {
          processRef.kill("SIGTERM");
          settleReject(new Error(`MCP command timed out after ${timeoutMs}ms`));
        }, timeoutMs);
      }

      processRef.on("close", (code, signal) => {
        const stdout = stdoutChunks.join("").trim();
        const stderr = stderrChunks.join("").trim();

        if (signal) {
          settleReject(new Error(`MCP command terminated by signal: ${signal}`));
          return;
        }

        if (code !== 0) {
          const detail = stderr || stdout || `exit code ${String(code)}`;
          settleReject(new Error(`MCP command failed: ${detail}`));
          return;
        }

        settleResolve(this.parseExecutionResult(stdout, stderr));
      });
    });
  }

  private resolveCommand(commandTemplate: string, payloadJson: string): CommandLayout {
    if (!commandTemplate.includes("{{payload}}")) {
      return {
        command: commandTemplate,
        useStdin: true
      };
    }

    return {
      command: commandTemplate.replaceAll("{{payload}}", shellEscape(payloadJson)),
      useStdin: false
    };
  }

  private parseExecutionResult(stdout: string, stderr: string): McpExecutionResult {
    const parsedJson = parseJsonFromOutput(stdout);
    const threadId = extractThreadId(parsedJson, stdout);

    let summary = "";
    if (parsedJson && typeof parsedJson.summary === "string") {
      summary = parsedJson.summary;
    } else if (parsedJson && typeof parsedJson.output === "string") {
      summary = parsedJson.output;
    } else {
      summary = stdout || stderr || "MCP command succeeded with empty output.";
    }

    return {
      threadId,
      summary,
      rawOutput: stdout || stderr
    };
  }
}

function shellEscape(value: string): string {
  return `'${value.replace(/'/g, `'"'"'`)}'`;
}

function parseJsonFromOutput(stdout: string): Record<string, unknown> | null {
  if (!stdout) {
    return null;
  }

  try {
    const parsed = JSON.parse(stdout) as unknown;
    if (isObject(parsed)) {
      return parsed;
    }
  } catch {
    // Continue with line-by-line fallback.
  }

  const lines = stdout
    .split(/\r?\n/u)
    .map((line) => line.trim())
    .filter((line) => line.length > 0);

  for (let index = lines.length - 1; index >= 0; index -= 1) {
    try {
      const parsed = JSON.parse(lines[index] ?? "") as unknown;
      if (isObject(parsed)) {
        return parsed;
      }
    } catch {
      // Ignore non-JSON lines.
    }
  }

  return null;
}

function extractThreadId(parsedJson: Record<string, unknown> | null, raw: string): string | null {
  if (parsedJson) {
    const candidates: Array<unknown> = [
      parsedJson.threadId,
      isObject(parsedJson.result) ? parsedJson.result.threadId : undefined,
      isObject(parsedJson.data) ? parsedJson.data.threadId : undefined
    ];

    for (const candidate of candidates) {
      if (typeof candidate === "string" && candidate.trim().length > 0) {
        return candidate;
      }
    }
  }

  const match = raw.match(/\bthr_[A-Za-z0-9_-]+\b/u);
  return match ? match[0] : null;
}

function isObject(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null;
}
