import os from "node:os";
import path from "node:path";
import { promises as fs } from "node:fs";

import { describe, expect, it } from "vitest";

import { FileSessionStore } from "../sessionStore.js";

describe("FileSessionStore", () => {
  it("loads null when no session file exists", async () => {
    const root = await fs.mkdtemp(path.join(os.tmpdir(), "session-store-empty-"));
    const store = new FileSessionStore(path.join(root, "session.json"));

    await expect(store.loadSession()).resolves.toBeNull();

    await fs.rm(root, { recursive: true, force: true });
  });

  it("saves and loads session", async () => {
    const root = await fs.mkdtemp(path.join(os.tmpdir(), "session-store-save-"));
    const store = new FileSessionStore(path.join(root, "session.json"));

    await store.saveSession({
      threadId: "thr_abc",
      cwd: "/workspaces/default",
      sandbox: "workspace-write",
      approvalPolicy: "on-request",
      updatedAt: "2026-02-21T12:00:00.000Z"
    });

    await expect(store.loadSession()).resolves.toEqual({
      threadId: "thr_abc",
      cwd: "/workspaces/default",
      sandbox: "workspace-write",
      approvalPolicy: "on-request",
      updatedAt: "2026-02-21T12:00:00.000Z"
    });

    await fs.rm(root, { recursive: true, force: true });
  });
});
