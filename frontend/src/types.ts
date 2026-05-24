/** WebSocket protocol types - mirror of backend src/ws messages. */

/** A model the active worker supports - sourced from the worker's SDK
 * (Copilot: `client.list_models()`) or a hub-side type default for
 * workers that don't expose a list endpoint. Frontend uses this to build
 * the /model autocomplete dynamically. */
export type ModelInfo = {
  id: string;
  name: string;
};

/** Worker the hub knows about (from workers.json). `connected` reflects
 * whether the hub has a live WS to it right now. Hub sends the full list
 * with every `projects` payload so the frontend can show workers even when
 * they have zero projects yet. */
export type WorkerInfo = {
  id: string;
  label: string;
  type: string;
  connected: boolean;
  /** Models the active worker accepts. May be empty briefly between
   * connect and the background list_models response landing. */
  models?: ModelInfo[];
};

export type Project = {
  id: number;
  path: string;
  name: string;
  auto_approve: boolean;
  created_at: string;
  last_seen_at: string;
  last_session_mtime: number | null;
  available: boolean;
  worker_id?: string;
  worker_label?: string;
  /** Agent SDK family the worker runs (e.g. "claude", "copilot"). Free-form
   * string. Frontend treats it cosmetically (badge + dropdown suffix). Missing
   * field (older hub) is treated as "claude". */
  worker_type?: string;
};

export type SessionSummary = {
  id: string;
  title: string | null;
  preview: string | null;
  message_count: number;
  mtime: number;
};

export type TaskEventType =
  | "started"
  | "progress"
  | "updated"
  | "notification";

export type ChatBlock =
  | { kind: "text"; text: string }
  | { kind: "thinking"; text: string }
  | { kind: "image"; media_type: string; data_b64: string }
  | { kind: "info"; text: string }
  | {
      kind: "tool";
      tool_use_id: string;
      name: string;
      input: Record<string, unknown>;
      result?: unknown;
      is_error?: boolean;
    }
  | {
      kind: "task";
      event_type: TaskEventType;
      task_id: string | null;
      summary: string | null;
      description: string | null;
      status: string | null;
      tool_use_id: string | null;
    };

export type PromptImage = {
  media_type: string;
  data_b64: string;
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
      payload: {
        project_id: number;
        session_id: string;
        limit?: number;
        before_uuid?: string | null;
      };
    }
  | { type: "new_session"; payload: { project_id: number; model?: string | null } }
  | {
      type: "resume_session";
      payload: { project_id: number; session_id: string; force?: boolean; model?: string | null };
    }
  | {
      type: "set_auto_approve";
      payload: { project_id: number; auto_approve: boolean };
    }
  | { type: "set_model"; payload: { model: string | null } }
  | {
      type: "prompt";
      payload: {
        text: string;
        auto_approve?: boolean;
        images?: PromptImage[];
        stream?: boolean;
      };
    }
  | { type: "interrupt"; payload: Record<string, never> }
  | { type: "approve_tool"; payload: { tool_use_id: string } }
  | { type: "deny_tool"; payload: { tool_use_id: string; reason?: string } }
  | { type: "list_directory"; payload: { project_id: number; path?: string } }
  | { type: "read_file"; payload: { project_id: number; path: string } }
  | { type: "write_file"; payload: { project_id: number; path: string; content: string } }
  | { type: "browse_fs"; payload: { path?: string; worker_id?: string } }
  | { type: "create_directory"; payload: { path: string; worker_id?: string } }
  | { type: "create_project"; payload: { path: string; worker_id?: string } }
  | {
      type: "user_input_response";
      payload: {
        tool_use_id: string;
        /** One answer per question (AskUserQuestion can pose multiple). */
        answers: string[];
      };
    }
  | { type: "terminal_open"; payload: { project_id: number; cols?: number; rows?: number } }
  | { type: "terminal_input"; payload: { data: string } }
  | { type: "terminal_resize"; payload: { cols: number; rows: number } }
  | { type: "terminal_close"; payload: Record<string, never> };

export type DirectoryEntry = {
  name: string;
  kind: "dir" | "file";
  size: number | null;
  mtime: number;
};

export type ServerMessage =
  | { type: "pong"; payload: Record<string, never> }
  | { type: "projects"; payload: { projects: Project[]; workers?: WorkerInfo[] } }
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
        has_more: boolean;
        oldest_uuid: string | null;
        before_uuid: string | null;
      };
    }
  | {
      type: "session_started";
      payload: {
        session_id: string;
        cwd: string | null;
        resumed: boolean;
        auto_approve: boolean;
        /** True when the session is currently mid-turn (e.g. reconnecting while streaming). */
        is_busy?: boolean;
      };
    }
  | {
      type: "permission_request";
      payload: {
        session_id: string;
        tool_use_id: string;
        name: string;
        input: Record<string, unknown>;
        description: string | null;
      };
    }
  | {
      type: "session_locked";
      payload: { session_id: string; locked_by: string; locked_at: number };
    }
  | { type: "lock_revoked"; payload: { session_id: string } }
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
      type: "task_event";
      payload: {
        session_id: string;
        event_type: TaskEventType;
        task_id: string | null;
        summary: string | null;
        description: string | null;
        status: string | null;
        tool_use_id: string | null;
        last_tool_name: string | null;
      };
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
  | {
      type: "directory";
      payload: {
        project_id: number;
        root: string;
        path: string;
        parent: string | null;
        entries: DirectoryEntry[];
      };
    }
  | {
      type: "file_content";
      payload: {
        project_id: number;
        path: string;
        size: number;
        too_large: boolean;
        encoding: "utf-8" | "base64" | null;
        content: string | null;
      };
    }
  | {
      type: "file_written";
      payload: {
        project_id: number;
        path: string;
        size: number;
      };
    }
  | { type: "error"; payload: { code: string; message: string } }
  | {
      type: "fs_directory";
      payload: {
        path: string;
        parent: string | null;
        entries: DirectoryEntry[];
      };
    }
  | {
      type: "project_created";
      payload: { project: Project };
    }
  | {
      type: "user_input_request";
      payload: {
        session_id: string;
        tool_use_id: string;
        /**
         * One or more questions to ask the user in this tool call.
         * AskUserQuestion can pose multiple distinct questions at once,
         * each with its own options and expecting its own answer.
         */
        questions: { question: string; options: string[] }[];
      };
    }
  | { type: "terminal_ready"; payload: Record<string, never> }
  | { type: "terminal_output"; payload: { data: string } }
  | { type: "terminal_closed"; payload: { exit_code: number } };
