import path from "node:path";

import { loadConfig } from "./config/env.js";
import { CommandMcpClient } from "./mcp/client.js";
import { FileJobStore } from "./state/jobStore.js";
import { FileLockManager } from "./state/lock.js";
import { FileLogStore } from "./state/logStore.js";
import { FileSessionStore } from "./state/sessionStore.js";
import { TelegramPollingBot } from "./telegram/bot.js";
import { TelegramCommandRouter } from "./telegram/commands.js";

async function main(): Promise<void> {
  const config = loadConfig();

  const sessionPath = path.join(config.stateDir, "session.json");
  const latestJobPath = path.join(config.stateDir, "jobs", "latest.json");
  const latestLogPath = path.join(config.stateDir, "logs", "latest.log");
  const lockPath = path.join(config.stateDir, "lock");

  const sessionStore = new FileSessionStore(sessionPath);
  const jobStore = new FileJobStore(latestJobPath);
  const logStore = new FileLogStore(latestLogPath);
  const lockManager = new FileLockManager(lockPath, config.staleLockOverrideMs);
  const mcpClient = new CommandMcpClient({
    runCommand: config.mcpRunCommand,
    replyCommand: config.mcpReplyCommand
  });

  const commandRouter = new TelegramCommandRouter({
    mcpClient,
    sessionStore,
    jobStore,
    logStore,
    lockManager,
    policy: {
      cwd: config.defaultCwd,
      sandbox: config.sandbox,
      approvalPolicy: config.approvalPolicy,
      timeoutMs: config.timeoutMs
    },
    logPath: latestLogPath,
    tailDefaultLines: config.tailDefaultLines,
    tailMaxLines: config.tailMaxLines
  });

  const bot = new TelegramPollingBot({
    token: config.telegramToken,
    pollTimeoutSeconds: config.pollTimeoutSeconds,
    pollIntervalMs: config.pollIntervalMs,
    allowedChatId: config.telegramAllowedChatId
  });

  process.on("SIGINT", () => bot.stop());
  process.on("SIGTERM", () => bot.stop());

  await bot.start(async (message) => commandRouter.handle(message.text));
}

main().catch((error) => {
  console.error("Application failed", error);
  process.exitCode = 1;
});
