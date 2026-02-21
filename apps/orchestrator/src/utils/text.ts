const DEFAULT_TELEGRAM_LIMIT = 3500;

export function truncateForTelegram(text: string, maxLength: number = DEFAULT_TELEGRAM_LIMIT): string {
  if (text.length <= maxLength) {
    return text;
  }

  const overflow = text.length - maxLength;
  const suffix = `\n... truncated (${overflow} chars)`;
  return `${text.slice(0, maxLength - suffix.length)}${suffix}`;
}

export function toErrorMessage(error: unknown): string {
  if (error instanceof Error) {
    return error.message;
  }

  if (typeof error === "string") {
    return error;
  }

  return "Unknown error";
}

export function parsePositiveInteger(value: string | undefined, fallback: number): number {
  if (!value) {
    return fallback;
  }

  const parsed = Number.parseInt(value, 10);
  if (!Number.isFinite(parsed) || parsed <= 0) {
    return fallback;
  }

  return parsed;
}

export function clamp(value: number, min: number, max: number): number {
  return Math.min(Math.max(value, min), max);
}
