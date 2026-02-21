import { sleep } from "../utils/time.js";
import { toErrorMessage } from "../utils/text.js";

export interface IncomingTelegramMessage {
  updateId: number;
  chatId: number;
  text: string;
}

export interface TelegramPollingBotConfig {
  token: string;
  pollTimeoutSeconds: number;
  pollIntervalMs: number;
  allowedChatId: number | null;
}

export type MessageHandler = (message: IncomingTelegramMessage) => Promise<string | null>;

interface TelegramApiResponse<T> {
  ok: boolean;
  description?: string;
  result: T;
}

interface TelegramUpdate {
  update_id: number;
  message?: {
    chat?: {
      id?: number;
    };
    text?: string;
  };
}

export class TelegramPollingBot {
  private readonly token: string;
  private readonly pollTimeoutSeconds: number;
  private readonly pollIntervalMs: number;
  private readonly allowedChatId: number | null;
  private offset = 0;
  private isRunning = true;

  constructor(config: TelegramPollingBotConfig) {
    this.token = config.token;
    this.pollTimeoutSeconds = config.pollTimeoutSeconds;
    this.pollIntervalMs = config.pollIntervalMs;
    this.allowedChatId = config.allowedChatId;
  }

  async start(handler: MessageHandler): Promise<void> {
    while (this.isRunning) {
      try {
        const updates = await this.getUpdates();
        for (const update of updates) {
          this.offset = update.update_id + 1;

          const message = this.toIncomingMessage(update);
          if (!message) {
            continue;
          }

          if (this.allowedChatId !== null && message.chatId !== this.allowedChatId) {
            continue;
          }

          let responseText: string | null = null;
          try {
            responseText = await handler(message);
          } catch (error) {
            responseText = `Command failed: ${toErrorMessage(error)}`;
          }

          if (responseText) {
            await this.sendMessage(message.chatId, responseText);
          }
        }
      } catch {
        if (!this.isRunning) {
          break;
        }
        await sleep(this.pollIntervalMs);
      }
    }
  }

  stop(): void {
    this.isRunning = false;
  }

  async sendMessage(chatId: number, text: string): Promise<void> {
    await this.callApi("sendMessage", {
      chat_id: chatId,
      text
    });
  }

  private async getUpdates(): Promise<TelegramUpdate[]> {
    const response = await this.callApi<TelegramUpdate[]>("getUpdates", {
      timeout: this.pollTimeoutSeconds,
      offset: this.offset,
      allowed_updates: ["message"]
    });

    return response;
  }

  private toIncomingMessage(update: TelegramUpdate): IncomingTelegramMessage | null {
    const chatId = update.message?.chat?.id;
    const text = update.message?.text;

    if (typeof chatId !== "number" || typeof text !== "string") {
      return null;
    }

    return {
      updateId: update.update_id,
      chatId,
      text
    };
  }

  private async callApi<T>(method: string, payload: Record<string, unknown>): Promise<T> {
    const url = `https://api.telegram.org/bot${this.token}/${method}`;

    const httpResponse = await fetch(url, {
      method: "POST",
      headers: {
        "content-type": "application/json"
      },
      body: JSON.stringify(payload)
    });

    if (!httpResponse.ok) {
      throw new Error(`Telegram API HTTP ${httpResponse.status}`);
    }

    const response = (await httpResponse.json()) as TelegramApiResponse<T>;
    if (!response.ok) {
      throw new Error(response.description ?? "Telegram API call failed");
    }

    return response.result;
  }
}
