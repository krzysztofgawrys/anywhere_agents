import { useCallback, useEffect, useRef, useState } from "react";
import { Composer } from "./components/Composer";
import { LockTakeoverModal } from "./components/LockTakeoverModal";
import { Message } from "./components/Message";
import { PermissionPrompt } from "./components/PermissionPrompt";
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
  const readOnly = useChatStore((s) => s.readOnly);
  const pendingLock = useChatStore((s) => s.pendingLock);
  const hasMore = useChatStore((s) => s.hasMore);
  const oldestUuid = useChatStore((s) => s.oldestUuid);
  const loadingOlder = useChatStore((s) => s.loadingOlder);
  const autoApprove = useChatStore((s) => s.autoApprove);
  const pendingPermissions = useChatStore((s) => s.pendingPermissions);
  const handleChatMsg = useChatStore((s) => s.handleServerMessage);
  const appendUserPrompt = useChatStore((s) => s.appendUserPrompt);
  const resetChat = useChatStore((s) => s.reset);
  const setHistory = useChatStore((s) => s.setHistory);
  const prependHistory = useChatStore((s) => s.prependHistory);
  const setLoadingOlder = useChatStore((s) => s.setLoadingOlder);
  const setPendingLock = useChatStore((s) => s.setPendingLock);
  const removePendingPermission = useChatStore((s) => s.removePendingPermission);

  const handleProjectsMsg = useProjectsStore((s) => s.handleServerMessage);

  const activeProjectIdRef = useRef<number | null>(null);
  useEffect(() => {
    const unsub = useProjectsStore.subscribe((s) => {
      activeProjectIdRef.current = s.activeProjectId;
    });
    return unsub;
  }, []);

  const onServerMessage = useCallback(
    (msg: ServerMessage) => {
      handleProjectsMsg(msg);
      if (msg.type === "session_history") {
        const isInitial = !msg.payload.before_uuid;
        if (isInitial) {
          setHistory(
            msg.payload.messages,
            msg.payload.has_more,
            msg.payload.oldest_uuid
          );
        } else {
          prependHistory(
            msg.payload.messages,
            msg.payload.has_more,
            msg.payload.oldest_uuid
          );
        }
      } else if (msg.type === "session_locked") {
        const projectId = activeProjectIdRef.current;
        if (projectId !== null) {
          setPendingLock({
            sessionId: msg.payload.session_id,
            lockedBy: msg.payload.locked_by,
            lockedAt: msg.payload.locked_at,
            projectId,
          });
        }
      } else {
        handleChatMsg(msg);
      }
    },
    [
      handleChatMsg,
      handleProjectsMsg,
      setHistory,
      prependHistory,
      setPendingLock,
    ]
  );

  const { connected, send } = useWebSocket({ onMessage: onServerMessage });

  const [sidebarOpen, setSidebarOpen] = useState(false);

  const scrollRef = useRef<HTMLDivElement>(null);
  const prevMessagesLengthRef = useRef(0);
  const nearBottomRef = useRef(true);
  useEffect(() => {
    const el = scrollRef.current;
    if (!el) return;
    if (loadingOlder) return;
    // Only auto-scroll if user was already near the bottom — don't yank away
    // if they're scrolling up to read older context.
    if (messages.length > prevMessagesLengthRef.current && nearBottomRef.current) {
      el.scrollTo({ top: el.scrollHeight, behavior: "smooth" });
    }
    prevMessagesLengthRef.current = messages.length;
  }, [messages, loadingOlder]);

  const onScroll = useCallback(() => {
    const el = scrollRef.current;
    if (!el) return;
    // Track whether user is near the bottom (for auto-scroll on new messages).
    nearBottomRef.current = el.scrollHeight - el.scrollTop - el.clientHeight < 120;
    if (el.scrollTop <= 30 && hasMore && !loadingOlder && oldestUuid && activeSessionId) {
      const projectId = activeProjectIdRef.current;
      if (projectId === null) return;
      setLoadingOlder(true);
      const prevHeight = el.scrollHeight;
      send({
        type: "session_history",
        payload: {
          project_id: projectId,
          session_id: activeSessionId,
          limit: 30,
          before_uuid: oldestUuid,
        },
      });
      requestAnimationFrame(() => {
        const el2 = scrollRef.current;
        if (!el2) return;
        const delta = el2.scrollHeight - prevHeight;
        el2.scrollTop = el2.scrollTop + delta;
      });
    }
  }, [hasMore, loadingOlder, oldestUuid, activeSessionId, send, setLoadingOlder]);

  const onSubmit = useCallback(
    (text: string, autoApproveOnce: boolean) => {
      appendUserPrompt(text);
      send({ type: "prompt", payload: { text, auto_approve: autoApproveOnce } });
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

  const onTakeover = useCallback(() => {
    if (!pendingLock || pendingLock.projectId === null) return;
    send({
      type: "resume_session",
      payload: {
        project_id: pendingLock.projectId,
        session_id: pendingLock.sessionId,
        force: true,
      },
    });
    setPendingLock(null);
  }, [pendingLock, send, setPendingLock]);

  const onAllowTool = useCallback(
    (toolUseId: string) => {
      send({ type: "approve_tool", payload: { tool_use_id: toolUseId } });
      removePendingPermission(toolUseId);
    },
    [send, removePendingPermission]
  );

  const onDenyTool = useCallback(
    (toolUseId: string) => {
      send({ type: "deny_tool", payload: { tool_use_id: toolUseId } });
      removePendingPermission(toolUseId);
    },
    [send, removePendingPermission]
  );

  const onToggleAutoApprove = useCallback(() => {
    const projectId = activeProjectIdRef.current;
    if (projectId === null) return;
    send({
      type: "set_auto_approve",
      payload: { project_id: projectId, auto_approve: !autoApprove },
    });
  }, [autoApprove, send]);

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
            {readOnly && (
              <span className="text-xs px-2 py-0.5 rounded bg-yellow-900/40 text-yellow-300 border border-yellow-700/60">
                read-only
              </span>
            )}
          </div>
          <div className="text-xs text-gray-500 shrink-0 flex items-center gap-3">
            {activeSessionId && (
              <button
                type="button"
                onClick={onToggleAutoApprove}
                className={`text-xs px-2 py-1 rounded border transition-colors ${
                  autoApprove
                    ? "bg-yellow-900/40 text-yellow-300 border-yellow-700/60 hover:bg-yellow-900/60"
                    : "bg-gray-800 text-gray-400 border-gray-700 hover:bg-gray-700"
                }`}
                title={autoApprove ? "Auto-approve ON — click to disable" : "Click to auto-approve every tool"}
              >
                {autoApprove ? "auto ON" : "auto OFF"}
              </button>
            )}
            {activeSessionId && (
              <span className="font-mono hidden md:inline" title={activeSessionId}>
                {activeSessionId.slice(0, 8)}
              </span>
            )}
            {cost > 0 && <span>${cost.toFixed(4)}</span>}
          </div>
        </header>

        <main
          ref={scrollRef}
          onScroll={onScroll}
          className="flex-1 overflow-y-auto px-3 md:px-6 py-4"
        >
          <div className="max-w-3xl mx-auto">
            {loadingOlder && (
              <div className="text-center text-xs text-gray-500 py-2">
                Loading older messages…
              </div>
            )}
            {!loadingOlder && hasMore && messages.length > 0 && (
              <div className="text-center text-xs text-gray-600 py-2">
                Scroll up to load older messages
              </div>
            )}
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

        {pendingPermissions.map((p) => (
          <PermissionPrompt
            key={p.toolUseId}
            request={p}
            onAllow={onAllowTool}
            onDeny={onDenyTool}
          />
        ))}

        <Composer
          disabled={!connected || !activeSessionId || readOnly}
          streaming={status === "streaming"}
          autoApproveActive={autoApprove}
          onSubmit={onSubmit}
          onInterrupt={onInterrupt}
        />
      </div>

      {pendingLock && (
        <LockTakeoverModal
          lock={pendingLock}
          onTakeover={onTakeover}
          onCancel={() => setPendingLock(null)}
        />
      )}
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
