import { promises as fs } from "node:fs";
import path from "node:path";

export async function ensureDirectory(dirPath: string, mode: number = 0o700): Promise<void> {
  await fs.mkdir(dirPath, { recursive: true, mode });
}

export async function writeTextFileAtomically(filePath: string, content: string, mode: number = 0o600): Promise<void> {
  const directory = path.dirname(filePath);
  await ensureDirectory(directory);

  const tempPath = `${filePath}.tmp-${process.pid}-${Date.now()}`;

  let handle;
  try {
    handle = await fs.open(tempPath, "w", mode);
    await handle.writeFile(content, { encoding: "utf8" });
    await handle.sync();
  } catch (error) {
    await fs.unlink(tempPath).catch(() => undefined);
    throw error;
  } finally {
    await handle?.close();
  }

  await fs.rename(tempPath, filePath);
  await fs.chmod(filePath, mode).catch(() => undefined);
  await syncDirectory(directory);
}

export async function atomicWriteJson(filePath: string, value: unknown, mode: number = 0o600): Promise<void> {
  const content = `${JSON.stringify(value, null, 2)}\n`;
  await writeTextFileAtomically(filePath, content, mode);
}

async function syncDirectory(directory: string): Promise<void> {
  let handle;
  try {
    handle = await fs.open(directory, "r");
    await handle.sync();
  } catch {
    // Directory sync is best-effort only.
  } finally {
    await handle?.close();
  }
}
