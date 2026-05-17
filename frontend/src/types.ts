/** WebSocket protocol types — mirror of backend src/ws messages. */

export type Project = {
  id: number;
  path: string;
  name: string;
  auto_approve: boolean;
  created_at: string;
  last_seen_at: string;
};

export type SessionSummary = {
  id: string;
  title: string | null;
  preview: string | null;
  message_count: number;
  mtime: number;
};

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

export type ClientMessage =
  | { type: "ping"; payload: Record<string, never> }
  | { type: "list_projects"; payload: Record<string, never> }
  | { type: "list_sessions"; payload: { project_id: number } }
  | {
      type: "session_history";
      payload: { project_id: number; session_id: string; limit?: number };
    }
  | { type: "new_session"; payload: { project_id: number } }
  | {
      type: "resume_session";
      payload: { project_id: number; session_id: string };
    }
  | {
      type: "set_auto_approve";
      payload: { project_id: number; auto_approve: boolean };
    }
  | { type: "prompt"; payload: { text: string; auto_approve?: boolean } }
  | { type: "interrupt"; payload: Record<string, never> };

export type ServerMessage =
  | { type: "pong"; payload: Record<string, never> }
  | { type: "projects"; payload: { projects: Project[] } }
  | {
      type: "sessions";
      payload: { project_id: number; sessions: SessionSummary[] };
    }
  | {
      type: "session_history";
      payload: {
        project_id: number;
        session_id: string;
        messages: ChatMessage[];
      };
    }
  | {
      type: "session_started";
      payload: { session_id: string; cwd: string | null; resumed: boolean };
    }
  | {
      type: "project_updated";
      payload: { project_id: number; auto_approve: boolean };
    }
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
