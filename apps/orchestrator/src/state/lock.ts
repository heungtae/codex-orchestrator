import { promises as fs } from "node:fs";
import path from "node:path";

import { ensureDirectory } from "./atomicWrite.js";

export interface LockInfo {
  pid: number;
  jobId: string;
  acquiredAt: string;
}

export class AlreadyLockedError extends Error {
  readonly lockInfo: LockInfo | null;

  constructor(lockInfo: LockInfo | null) {
    super(lockInfo ? `Already running job ${lockInfo.jobId}` : "Another job is already running");
    this.name = "AlreadyLockedError";
    this.lockInfo = lockInfo;
  }
}

export interface LockManager {
  acquireLock(info: LockInfo): Promise<void>;
  releaseLock(): Promise<void>;
}

export class FileLockManager implements LockManager {
  private readonly lockPath: string;
  private readonly staleLockOverrideMs: number;

  constructor(lockPath: string, staleLockOverrideMs: number = 0) {
    this.lockPath = lockPath;
    this.staleLockOverrideMs = staleLockOverrideMs;
  }

  async acquireLock(info: LockInfo): Promise<void> {
    await ensureDirectory(path.dirname(this.lockPath));
    await this.tryAcquire(info, true);
  }

  async releaseLock(): Promise<void> {
    await fs.unlink(this.lockPath).catch(() => undefined);
  }

  private async tryAcquire(info: LockInfo, allowStaleRecovery: boolean): Promise<void> {
    let handle;

    try {
      handle = await fs.open(this.lockPath, "wx", 0o600);
      await handle.writeFile(`${JSON.stringify(info, null, 2)}\n`, "utf8");
      await handle.sync();
    } catch (error) {
      if (!isAlreadyExistsError(error)) {
        throw error;
      }

      const existing = await this.loadCurrentLock();
      if (
        allowStaleRecovery &&
        this.staleLockOverrideMs > 0 &&
        existing &&
        isStale(existing.acquiredAt, this.staleLockOverrideMs)
      ) {
        await fs.unlink(this.lockPath).catch(() => undefined);
        await this.tryAcquire(info, false);
        return;
      }

      throw new AlreadyLockedError(existing);
    } finally {
      await handle?.close();
    }
  }

  private async loadCurrentLock(): Promise<LockInfo | null> {
    try {
      const raw = await fs.readFile(this.lockPath, "utf8");
      const parsed = JSON.parse(raw) as unknown;

      if (
        isObject(parsed) &&
        typeof parsed.pid === "number" &&
        typeof parsed.jobId === "string" &&
        typeof parsed.acquiredAt === "string"
      ) {
        return {
          pid: parsed.pid,
          jobId: parsed.jobId,
          acquiredAt: parsed.acquiredAt
        };
      }

      return null;
    } catch (error) {
      if (isFileNotFoundError(error)) {
        return null;
      }
      throw error;
    }
  }
}

function isStale(acquiredAt: string, staleLockOverrideMs: number): boolean {
  const acquiredTimestamp = Date.parse(acquiredAt);
  if (Number.isNaN(acquiredTimestamp)) {
    return false;
  }

  return Date.now() - acquiredTimestamp > staleLockOverrideMs;
}

function isAlreadyExistsError(error: unknown): boolean {
  return error instanceof Error && "code" in error && (error as NodeJS.ErrnoException).code === "EEXIST";
}

function isFileNotFoundError(error: unknown): boolean {
  return error instanceof Error && "code" in error && (error as NodeJS.ErrnoException).code === "ENOENT";
}

function isObject(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null;
}
