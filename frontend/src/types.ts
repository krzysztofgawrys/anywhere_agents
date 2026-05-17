/** WebSocket protocol types — mirror of backend src/ws messages. */

export type ClientMessage =
  | { type: "ping"; payload: Record<string, never> }
  | { type: "prompt"; payload: { text: string; auto_approve?: boolean } }
  | { type: "interrupt"; payload: { session_id?: string } };

export type ServerMessage =
  | { type: "pong"; payload: Record<string, never> }
  | { type: "session_started"; payload: { session_id: string; history: unknown[] } }
  | { type: "text_delta"; payload: { session_id: string; text: string } }
  | { type: "thinking"; payload: { session_id: string; text: string } }
  | {
      type: "tool_call";
      payload: {
        session_id: string;
        tool_use_id: string;
        name: string;
        input: Record<string, unknown>;
      };
    }
  | {
      type: "tool_result";
      payload: {
        session_id: string;
        tool_use_id: string;
        content: unknown;
        is_error: boolean;
      };
    }
  | {
      type: "system";
      payload: { session_id: string; subtype: string; data: unknown };
    }
  | {
      type: "result";
      payload: {
        session_id: string;
        subtype: string;
        duration_ms: number;
        total_cost_usd: number;
        num_turns: number;
        is_error: boolean;
      };
    }
  | { type: "error"; payload: { code: string; message: string } };

/** UI-level chat message — accumulated from server messages. */
export type ChatBlock =
  | { kind: "text"; text: string }
  | { kind: "thinking"; text: string }
  | {
      kind: "tool";
      tool_use_id: string;
      name: string;
      input: Record<string, unknown>;
      result?: unknown;
      is_error?: boolean;
    };

export type ChatMessage = {
  id: string;
  role: "user" | "assistant" | "system";
  blocks: ChatBlock[];
  finished?: boolean;
};
