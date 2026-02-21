import os from "node:os";
import path from "node:path";
import { promises as fs } from "node:fs";

import { describe, expect, it } from "vitest";

import { atomicWriteJson } from "../atomicWrite.js";

describe("atomicWriteJson", () => {
  it("writes JSON atomically to target path", async () => {
    const root = await fs.mkdtemp(path.join(os.tmpdir(), "atomic-write-test-"));
    const filePath = path.join(root, "nested", "session.json");

    await atomicWriteJson(filePath, { threadId: "thr_123" });

    const raw = await fs.readFile(filePath, "utf8");
    expect(JSON.parse(raw)).toEqual({ threadId: "thr_123" });

    await fs.rm(root, { recursive: true, force: true });
  });
});
