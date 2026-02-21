import { promises as fs } from "node:fs";
import path from "node:path";

import { ensureDirectory, writeTextFileAtomically } from "./atomicWrite.js";

export interface LogStore {
  writeLatestLog(content: string): Promise<void>;
  appendLatestLog(content: string): Promise<void>;
  tailLatestLog(lineCount: number): Promise<string>;
}

export class FileLogStore implements LogStore {
  private readonly logPath: string;

  constructor(logPath: string) {
    this.logPath = logPath;
  }

  async writeLatestLog(content: string): Promise<void> {
    await writeTextFileAtomically(this.logPath, content, 0o600);
  }

  async appendLatestLog(content: string): Promise<void> {
    await ensureDirectory(path.dirname(this.logPath));
    await fs.appendFile(this.logPath, content, { encoding: "utf8", mode: 0o600 });
  }

  async tailLatestLog(lineCount: number): Promise<string> {
    try {
      const raw = await fs.readFile(this.logPath, "utf8");
      const lines = raw.split(/\r?\n/u);
      if (lines.length > 0 && lines[lines.length - 1] === "") {
        lines.pop();
      }

      const trimmed = lines.slice(-lineCount);
      return trimmed.join("\n");
    } catch (error) {
      if (isFileNotFoundError(error)) {
        return "";
      }
      throw error;
    }
  }
}

function isFileNotFoundError(error: unknown): boolean {
  return error instanceof Error && "code" in error && (error as NodeJS.ErrnoException).code === "ENOENT";
}
