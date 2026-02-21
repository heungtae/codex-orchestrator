export interface ExecutionPolicy {
  cwd: string;
  sandbox: string;
  approvalPolicy: string;
  timeoutMs: number;
}

export interface McpRunRequest {
  prompt: string;
  policy: ExecutionPolicy;
}

export interface McpReplyRequest extends McpRunRequest {
  threadId: string;
}

export interface McpExecutionResult {
  threadId: string | null;
  summary: string;
  rawOutput: string;
}

export interface McpClient {
  run(request: McpRunRequest): Promise<McpExecutionResult>;
  reply(request: McpReplyRequest): Promise<McpExecutionResult>;
}
