export function nowIso(): string {
  return new Date().toISOString();
}

function pad(value: number): string {
  return String(value).padStart(2, "0");
}

export function createJobId(at: Date = new Date()): string {
  const yyyy = at.getFullYear();
  const mm = pad(at.getMonth() + 1);
  const dd = pad(at.getDate());
  const hh = pad(at.getHours());
  const min = pad(at.getMinutes());
  const ss = pad(at.getSeconds());
  return `job_${yyyy}${mm}${dd}_${hh}${min}${ss}`;
}

export function sleep(ms: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, ms));
}
