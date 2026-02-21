import os from "node:os";
import path from "node:path";
import { promises as fs } from "node:fs";

import { describe, expect, it } from "vitest";

import { AlreadyLockedError, FileLockManager } from "../lock.js";

describe("FileLockManager", () => {
  it("prevents concurrent acquisition", async () => {
    const root = await fs.mkdtemp(path.join(os.tmpdir(), "lock-store-test-"));
    const lockPath = path.join(root, "lock");
    const manager = new FileLockManager(lockPath);

    await manager.acquireLock({
      pid: 100,
      jobId: "job_1",
      acquiredAt: "2026-02-21T12:00:00.000Z"
    });

    await expect(
      manager.acquireLock({
        pid: 200,
        jobId: "job_2",
        acquiredAt: "2026-02-21T12:00:01.000Z"
      })
    ).rejects.toBeInstanceOf(AlreadyLockedError);

    await manager.releaseLock();
    await fs.rm(root, { recursive: true, force: true });
  });

  it("overrides stale lock when enabled", async () => {
    const root = await fs.mkdtemp(path.join(os.tmpdir(), "lock-store-stale-"));
    const lockPath = path.join(root, "lock");

    await fs.mkdir(root, { recursive: true });
    await fs.writeFile(
      lockPath,
      JSON.stringify({
        pid: 123,
        jobId: "job_old",
        acquiredAt: "2000-01-01T00:00:00.000Z"
      })
    );

    const manager = new FileLockManager(lockPath, 1000);

    await expect(
      manager.acquireLock({
        pid: 777,
        jobId: "job_new",
        acquiredAt: new Date().toISOString()
      })
    ).resolves.toBeUndefined();

    await manager.releaseLock();
    await fs.rm(root, { recursive: true, force: true });
  });
});
