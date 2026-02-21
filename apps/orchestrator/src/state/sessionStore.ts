import { promises as fs } from "node:fs";

import { atomicWriteJson } from "./atomicWrite.js";

export interface SessionState {
  threadId: string;
  cwd: string;
  sandbox: string;
  approvalPolicy: string;
  updatedAt: string;
}

export interface SessionStore {
  loadSession(): Promise<SessionState | null>;
  saveSession(session: SessionState): Promise<void>;
}

export class FileSessionStore implements SessionStore {
  private readonly sessionPath: string;

  constructor(sessionPath: string) {
    this.sessionPath = sessionPath;
  }

  async loadSession(): Promise<SessionState | null> {
    try {
      const raw = await fs.readFile(this.sessionPath, "utf8");
      const parsed = JSON.parse(raw) as unknown;
      if (!isSessionState(parsed)) {
        throw new Error(`Invalid session schema at ${this.sessionPath}`);
      }
      return parsed;
    } catch (error) {
      if (isFileNotFoundError(error)) {
        return null;
      }
      throw error;
    }
  }

  async saveSession(session: SessionState): Promise<void> {
    await atomicWriteJson(this.sessionPath, session);
  }
}

function isSessionState(value: unknown): value is SessionState {
  if (!isObject(value)) {
    return false;
  }

  return (
    typeof value.threadId === "string" &&
    typeof value.cwd === "string" &&
    typeof value.sandbox === "string" &&
    typeof value.approvalPolicy === "string" &&
    typeof value.updatedAt === "string"
  );
}

function isObject(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null;
}

function isFileNotFoundError(error: unknown): boolean {
  return error instanceof Error && "code" in error && (error as NodeJS.ErrnoException).code === "ENOENT";
}
