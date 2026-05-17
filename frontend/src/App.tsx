import { useCallback, useEffect, useRef, useState } from "react";
import { Composer } from "./components/Composer";
import { Message } from "./components/Message";
import { Sidebar } from "./components/Sidebar";
import { useWebSocket } from "./hooks/useWebSocket";
import { useChatStore } from "./stores/chat";
import { useProjectsStore } from "./stores/projects";
import type { ServerMessage } from "./types";

function App() {
  const messages = useChatStore((s) => s.messages);
  const status = useChatStore((s) => s.status);
  const lastError = useChatStore((s) => s.lastError);
  const cost = useChatStore((s) => s.cost);
  const activeSessionId = useChatStore((s) => s.activeSessionId);
  const activeCwd = useChatStore((s) => s.activeCwd);
  const handleChatMsg = useChatStore((s) => s.handleServerMessage);
  const appendUserPrompt = useChatStore((s) => s.appendUserPrompt);
  const resetChat = useChatStore((s) => s.reset);
  const loadHistory = useChatStore((s) => s.loadHistory);

  const handleProjectsMsg = useProjectsStore((s) => s.handleServerMessage);

  // Single message handler — fan out to all stores
  const onServerMessage = useCallback(
    (msg: ServerMessage) => {
      handleProjectsMsg(msg);
      // history is consumed here, not in chat store
      if (msg.type === "session_history") {
        loadHistory(msg.payload.messages);
      } else {
        handleChatMsg(msg);
      }
    },
    [handleChatMsg, handleProjectsMsg, loadHistory]
  );

  const { connected, send } = useWebSocket({ onMessage: onServerMessage });

  const [sidebarOpen, setSidebarOpen] = useState(false);

  const scrollRef = useRef<HTMLDivElement>(null);
  useEffect(() => {
    scrollRef.current?.scrollTo({
      top: scrollRef.current.scrollHeight,
      behavior: "smooth",
    });
  }, [messages]);

  const onSubmit = useCallback(
    (text: string) => {
      appendUserPrompt(text);
      send({ type: "prompt", payload: { text } });
    },
    [send, appendUserPrompt]
  );

  const onInterrupt = useCallback(() => {
    send({ type: "interrupt", payload: {} });
  }, [send]);

  const onNewSession = useCallback(
    (projectId: number) => {
      resetChat();
      send({ type: "new_session", payload: { project_id: projectId } });
    },
    [resetChat, send]
  );

  const onPickSession = useCallback(
    (projectId: number, sessionId: string) => {
      resetChat();
      // Load history then start the SDK session
      send({
        type: "session_history",
        payload: { project_id: projectId, session_id: sessionId, limit: 30 },
      });
      send({
        type: "resume_session",
        payload: { project_id: projectId, session_id: sessionId },
      });
    },
    [resetChat, send]
  );

  return (
    <div className="h-screen flex bg-gray-900 text-gray-100">
      <Sidebar
        send={send}
        connected={connected}
        open={sidebarOpen}
        onClose={() => setSidebarOpen(false)}
        onPickSession={onPickSession}
        onNewSession={onNewSession}
      />

      <div className="flex-1 flex flex-col min-w-0">
        <header className="border-b border-gray-800 px-3 md:px-4 py-2 md:py-3 flex items-center justify-between gap-3">
          <div className="flex items-center gap-2 md:gap-3 min-w-0">
            <button
              type="button"
              onClick={() => setSidebarOpen(true)}
              className="md:hidden p-1 -ml-1 text-gray-400 hover:text-white"
              aria-label="Open sidebar"
            >
              <svg width="20" height="20" viewBox="0 0 20 20" fill="currentColor">
                <path d="M3 5h14v2H3zm0 4h14v2H3zm0 4h14v2H3z" />
              </svg>
            </button>
            <h1 className="text-base md:text-lg font-semibold truncate">
              {activeCwd ? truncatePath(activeCwd) : "Claude Web"}
            </h1>
            <div className="flex items-center gap-1.5 shrink-0">
              <div
                className={`w-2 h-2 rounded-full ${
                  connected ? "bg-green-400" : "bg-red-400"
                }`}
              />
            </div>
          </div>
          <div className="text-xs text-gray-500 shrink-0 flex items-center gap-3">
            {activeSessionId && (
              <span className="font-mono hidden md:inline" title={activeSessionId}>
                {activeSessionId.slice(0, 8)}
              </span>
            )}
            {cost > 0 && <span>${cost.toFixed(4)}</span>}
          </div>
        </header>

        <main ref={scrollRef} className="flex-1 overflow-y-auto px-3 md:px-6 py-4">
          <div className="max-w-3xl mx-auto">
            {!activeSessionId && messages.length === 0 && (
              <EmptyState onOpenSidebar={() => setSidebarOpen(true)} />
            )}
            {messages.map((m) => (
              <Message key={m.id} message={m} />
            ))}
            {lastError && (
              <div className="text-xs text-red-400 mt-2 px-3 py-2 rounded bg-red-950/40 border border-red-800">
                {lastError}
              </div>
            )}
          </div>
        </main>

        <Composer
          disabled={!connected || !activeSessionId}
          streaming={status === "streaming"}
          onSubmit={onSubmit}
          onInterrupt={onInterrupt}
        />
      </div>
    </div>
  );
}

function EmptyState({ onOpenSidebar }: { onOpenSidebar: () => void }) {
  return (
    <div className="text-center text-gray-500 mt-12 md:mt-24">
      <h2 className="text-lg text-gray-300 mb-2">Pick a project to start</h2>
      <p className="text-sm">
        Open the sidebar and choose a project, then start a new session or pick
        an existing one.
      </p>
      <button
        type="button"
        onClick={onOpenSidebar}
        className="md:hidden mt-4 px-4 py-2 rounded bg-blue-600 hover:bg-blue-700 text-white text-sm"
      >
        Open projects
      </button>
    </div>
  );
}

function truncatePath(path: string): string {
  const parts = path.split("/").filter(Boolean);
  if (parts.length <= 2) return path;
  return ".../" + parts.slice(-2).join("/");
}

export default App;
