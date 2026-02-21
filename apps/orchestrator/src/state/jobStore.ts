import { promises as fs } from "node:fs";

import { atomicWriteJson } from "./atomicWrite.js";

export type JobStatus = "queued" | "running" | "succeeded" | "failed";

export interface JobRecord {
  jobId: string;
  status: JobStatus;
  startedAt: string;
  endedAt: string | null;
  threadId: string | null;
  instruction: string;
  logPath: string;
  error: string | null;
}

export interface JobStore {
  loadLatestJob(): Promise<JobRecord | null>;
  saveLatestJob(job: JobRecord): Promise<void>;
}

export class FileJobStore implements JobStore {
  private readonly latestJobPath: string;

  constructor(latestJobPath: string) {
    this.latestJobPath = latestJobPath;
  }

  async loadLatestJob(): Promise<JobRecord | null> {
    try {
      const raw = await fs.readFile(this.latestJobPath, "utf8");
      const parsed = JSON.parse(raw) as unknown;
      if (!isJobRecord(parsed)) {
        throw new Error(`Invalid job schema at ${this.latestJobPath}`);
      }
      return parsed;
    } catch (error) {
      if (isFileNotFoundError(error)) {
        return null;
      }
      throw error;
    }
  }

  async saveLatestJob(job: JobRecord): Promise<void> {
    await atomicWriteJson(this.latestJobPath, job);
  }
}

function isJobRecord(value: unknown): value is JobRecord {
  if (!isObject(value)) {
    return false;
  }

  return (
    typeof value.jobId === "string" &&
    (value.status === "queued" || value.status === "running" || value.status === "succeeded" || value.status === "failed") &&
    typeof value.startedAt === "string" &&
    (typeof value.endedAt === "string" || value.endedAt === null) &&
    (typeof value.threadId === "string" || value.threadId === null) &&
    typeof value.instruction === "string" &&
    typeof value.logPath === "string" &&
    (typeof value.error === "string" || value.error === null)
  );
}

function isObject(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null;
}

function isFileNotFoundError(error: unknown): boolean {
  return error instanceof Error && "code" in error && (error as NodeJS.ErrnoException).code === "ENOENT";
}
