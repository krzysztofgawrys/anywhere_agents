import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import { Composer } from "./components/Composer";
import { FileBrowser } from "./components/FileBrowser";
import { LockTakeoverModal } from "./components/LockTakeoverModal";
import { Message } from "./components/Message";
import { NewProjectBrowser } from "./components/NewProjectBrowser";
import { PermissionPrompt } from "./components/PermissionPrompt";
import { Terminal } from "./components/Terminal";
import { UserInputPrompt } from "./components/UserInputPrompt";
import { Sidebar } from "./components/Sidebar";
import { StreamingStatus } from "./components/StreamingStatus";
import { TypingIndicator } from "./components/TypingIndicator";
import { usePushNotifications } from "./hooks/usePushNotifications";
import { useVisibilityNotify } from "./hooks/useVisibilityNotify";
import { useWakeLock } from "./hooks/useWakeLock";
import { useWebSocket } from "./hooks/useWebSocket";
import { useChatStore } from "./stores/chat";
import { useFilesStore } from "./stores/files";
import { useProjectsStore } from "./stores/projects";
import type { ClientMessage, DirectoryEntry, Project, PromptImage, ServerMessage } from "./types";

function App() {
  const messages = useChatStore((s) => s.messages);
  const status = useChatStore((s) => s.status);
  const currentAssistantId = useChatStore((s) => s.currentAssistantId);
  const lastError = useChatStore((s) => s.lastError);
  const activeSessionId = useChatStore((s) => s.activeSessionId);
  const activeCwd = useChatStore((s) => s.activeCwd);
  const readOnly = useChatStore((s) => s.readOnly);
  const pendingLock = useChatStore((s) => s.pendingLock);
  const hasMore = useChatStore((s) => s.hasMore);
  const oldestUuid = useChatStore((s) => s.oldestUuid);
  const loadingOlder = useChatStore((s) => s.loadingOlder);
  const autoApprove = useChatStore((s) => s.autoApprove);
  const planMode = useChatStore((s) => s.planMode);
  const setPlanMode = useChatStore((s) => s.setPlanMode);
  const selectedModel = useChatStore((s) => s.model);
  const streamingActivity = useChatStore((s) => s.streamingActivity);
  const pendingPermissions = useChatStore((s) => s.pendingPermissions);
  const pendingUserInputs = useChatStore((s) => s.pendingUserInputs);
  const handleChatMsg = useChatStore((s) => s.handleServerMessage);
  const appendUserPrompt = useChatStore((s) => s.appendUserPrompt);
  const resetChat = useChatStore((s) => s.reset);
  const setHistory = useChatStore((s) => s.setHistory);
  const prependHistory = useChatStore((s) => s.prependHistory);
  const setLoadingOlder = useChatStore((s) => s.setLoadingOlder);
  const setPendingLock = useChatStore((s) => s.setPendingLock);
  const removePendingPermission = useChatStore((s) => s.removePendingPermission);
  const removeUserInput = useChatStore((s) => s.removeUserInput);

  const handleProjectsMsg = useProjectsStore((s) => s.handleServerMessage);
  const activeProjectId = useProjectsStore((s) => s.activeProjectId);
  const setActiveProject = useProjectsStore((s) => s.setActive);
  const projects = useProjectsStore((s) => s.projects);
  const workersList = useProjectsStore((s) => s.workers);
  const handleFilesMsg = useFilesStore((s) => s.handleServerMessage);
  const openFiles = useFilesStore((s) => s.open);

  const [newProjectOpen, setNewProjectOpen] = useState(false);
  const [newProjectWorkerId, setNewProjectWorkerId] = useState<string | undefined>();
  const [fsDirectory, setFsDirectory] = useState<{
    path: string;
    parent: string | null;
    entries: DirectoryEntry[];
  } | null>(null);

  // Which AskUserQuestion panels are minimized (keyed by tool_use_id). Lifted
  // here so the composer can be hidden whenever a panel is expanded - see
  // anyUserInputExpanded below.
  const [minimizedInputs, setMinimizedInputs] = useState<Set<string>>(new Set());
  const toggleInputMinimized = useCallback((toolUseId: string) => {
    setMinimizedInputs((prev) => {
      const next = new Set(prev);
      if (next.has(toolUseId)) next.delete(toolUseId);
      else next.add(toolUseId);
      return next;
    });
  }, []);
  // Clean up stale entries when a pending input is removed (answered/dismissed).
  useEffect(() => {
    const live = new Set(pendingUserInputs.map((p) => p.toolUseId));
    setMinimizedInputs((prev) => {
      let changed = false;
      const next = new Set<string>();
      for (const id of prev) {
        if (live.has(id)) next.add(id);
        else changed = true;
      }
      return changed ? next : prev;
    });
  }, [pendingUserInputs]);
  const anyUserInputExpanded = pendingUserInputs.some(
    (p) => !minimizedInputs.has(p.toolUseId)
  );

  const activeProjectIdRef = useRef<number | null>(null);
  // True while we expect a `result` event whose arrival should trigger a fresh
  // session_history refetch. Set on WS reconnect (resumeLastSession) when the
  // session may have produced messages during the disconnect window that the
  // SDK hadn't flushed to JSONL yet at the time of our initial history fetch.
  // Cleared once we refetch (or after a fallback timeout).
  const pendingHistoryRefreshRef = useRef(false);
  // Last `prompt` message the user submitted - retained so we can auto-resend
  // when a worker restart drops its in-memory session and the next prompt
  // bounces back with `no_session`. Cleared after a successful resend attempt
  // so a second `no_session` doesn't loop.
  const lastPromptRef = useRef<ClientMessage | null>(null);
  useEffect(() => {
    const unsub = useProjectsStore.subscribe((s) => {
      activeProjectIdRef.current = s.activeProjectId;
    });
    return unsub;
  }, []);

  const notifyIfHidden = useVisibilityNotify();
  const { requestSubscription, hasPushSubscription } = usePushNotifications();

  const streaming = status === "streaming";
  useWakeLock(streaming);

  const LS_KEY = "claude_web_last_session";

  const persistSession = useCallback((projectId: number, sessionId: string) => {
    try {
      localStorage.setItem(LS_KEY, JSON.stringify({ projectId, sessionId }));
    } catch {}
  }, []);

  // Terminal state - must be declared before onServerMessage uses killTerminal.
  const [terminalMounted, setTerminalMounted] = useState(false);
  const [terminalOpen, setTerminalOpen] = useState(false);
  const terminalWriteRef = useRef<((b64: string) => void) | null>(null);
  const killTerminal = useCallback(() => {
    setTerminalMounted(false);
    setTerminalOpen(false);
    terminalWriteRef.current = null;
  }, []);

  const onServerMessage = useCallback(
    (msg: ServerMessage) => {
      if (msg.type === "error" && msg.payload.code === "no_session") {
        // Worker lost its in-memory session (typically: worker container was
        // restarted, the parked-session registry was wiped). Our WS to the
        // hub is still healthy and the project routing is intact, so a simple
        // resume_session re-attaches via the SDK slow path (replay from
        // JSONL on disk), then we re-send the user's prompt so they don't
        // have to retype it.
        const sessionId = useChatStore.getState().activeSessionId;
        const projectId = activeProjectIdRef.current;
        const pending = lastPromptRef.current;
        if (sessionId && projectId !== null && pending) {
          // Drop the pending payload first so a second no_session bounce
          // doesn't loop us.
          lastPromptRef.current = null;
          sendRef.current?.({
            type: "resume_session",
            payload: { project_id: projectId, session_id: sessionId },
          });
          sendRef.current?.(pending);
          // Swallow the error - the user shouldn't see a red banner for a
          // condition we recovered from automatically.
          return;
        }
      }
      if (msg.type === "result" && !msg.payload.is_error) {
        // First result ever → good moment to ask for notification permission,
        // the user just saw Claude finish something and understands why.
        requestSubscription();
        // Skip local notification when Web Push is active - the server
        // already sends a push_notify that the SW displays.
        if (!hasPushSubscription) {
          notifyIfHidden("Claude finished", "Task completed - tap to view.");
        }
      }
      if (msg.type === "result" && pendingHistoryRefreshRef.current) {
        // A turn just finished after a WS reconnect - the SDK has now
        // flushed the in-flight turn to JSONL, so refetch history to pick
        // up any messages produced during the disconnect window.
        pendingHistoryRefreshRef.current = false;
        const projectId = activeProjectIdRef.current;
        const sessionId = useChatStore.getState().activeSessionId;
        if (projectId !== null && sessionId !== null) {
          sendRef.current?.({
            type: "session_history",
            payload: { project_id: projectId, session_id: sessionId, limit: 30 },
          });
        }
      }
      if (msg.type === "session_started" && pendingHistoryRefreshRef.current) {
        // Reconnect responded - if the session was already idle, the initial
        // history fetch is authoritative (no in-flight turn to wait on).
        // Clear the flag so we don't refetch on a future unrelated `result`.
        if (!msg.payload.is_busy) {
          pendingHistoryRefreshRef.current = false;
        }
      }
      handleProjectsMsg(msg);
      handleFilesMsg(msg);
      if (msg.type === "directory" || msg.type === "file_content" || msg.type === "file_written") {
        // Already handled by the files store - don't push into chat.
        return;
      }
      if (msg.type === "fs_directory") {
        setFsDirectory(msg.payload);
        return;
      }
      if (msg.type === "project_created") {
        // projects list is refreshed by the backend sending a "projects" message
        // immediately after; close the picker.
        setNewProjectOpen(false);
        setFsDirectory(null);
        return;
      }
      if (msg.type === "terminal_output") {
        terminalWriteRef.current?.(msg.payload.data);
        return;
      }
      if (msg.type === "terminal_ready") {
        return;
      }
      if (msg.type === "terminal_closed") {
        // Process exited (e.g. user typed `exit`) - tear down the panel.
        killTerminal();
        return;
      }
      if (msg.type === "session_started") {
        // Persist session so a fresh page load (e.g. from push notification tap)
        // can auto-resume it without requiring user interaction.
        const projectId = activeProjectIdRef.current;
        if (projectId !== null) {
          persistSession(projectId, msg.payload.session_id);
        }
        if (!msg.payload.resumed) {
          // New session created - refresh the session list so it shows up
          // immediately in the sidebar without requiring a page reload.
          if (projectId !== null) {
            send({ type: "list_sessions", payload: { project_id: projectId } });
          }
        }
      }
      if (msg.type === "session_history") {
        // Restore active project on reconnect (project_id is always in the payload).
        setActiveProject(msg.payload.project_id);
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
      requestSubscription,
      hasPushSubscription,
      notifyIfHidden,
      persistSession,
      handleChatMsg,
      handleProjectsMsg,
      handleFilesMsg,
      setHistory,
      prependHistory,
      setPendingLock,
      setFsDirectory,
      setNewProjectOpen,
      setActiveProject,
      killTerminal,
    ]
  );

  const onBrowseFiles = useCallback(
    (project: Project) => {
      openFiles({ id: project.id, name: project.name, path: project.path });
    },
    [openFiles]
  );

  // sendRef lets onReconnect close over the stable ref rather than the
  // not-yet-declared `send` value - defined before useWebSocket.
  const sendRef = useRef<((msg: ClientMessage) => boolean) | null>(null);

  /** Try to resume: prefer in-memory state, fall back to localStorage.
   *
   * Only sends resume_session when the page is visible - if the tab is in the
   * background, Chrome may briefly reconnect the WS (power management) but we
   * must NOT reclaim the parked session, otherwise the backend push never fires.
   */
  const resumeLastSession = useCallback(() => {
    let projectId = activeProjectIdRef.current;
    let sessionId = useChatStore.getState().activeSessionId;

    // Fresh page load - nothing in memory, check localStorage.
    if (projectId === null || sessionId === null) {
      try {
        const saved = localStorage.getItem(LS_KEY);
        if (saved) {
          const parsed = JSON.parse(saved) as { projectId: number; sessionId: string };
          projectId = parsed.projectId;
          sessionId = parsed.sessionId;
        }
      } catch {}
    }

    if (projectId === null || sessionId === null) return;

    if (!document.hidden) {
      // Page is visible - claim the session lock and reload history.
      sendRef.current?.({
        type: "resume_session",
        payload: { project_id: projectId, session_id: sessionId },
      });
    }
    // Always reload history so background output becomes visible when user returns.
    sendRef.current?.({
      type: "session_history",
      payload: { project_id: projectId, session_id: sessionId, limit: 30 },
    });
  }, []);

  const onConnect = useCallback(() => {
    // Fires only on the first WS connection of this page load (e.g. user tapped
    // a push notification and opened a fresh tab). Resume from localStorage.
    resumeLastSession();
  }, [resumeLastSession]);

  const onReconnect = useCallback(() => {
    // WS re-established after a drop while the tab was open. Prefer in-memory
    // state; the backend may have the session parked in its registry.
    //
    // During the disconnect window the SDK may have produced new events that
    // didn't reach our (dead) WS - the worker silently dropped them. If a
    // turn ended in that window the SDK has by now flushed to JSONL, but the
    // initial session_history we send below may race with the flush. Arm a
    // refetch on the first `result` event so we pick up the complete state
    // once the in-flight turn (if any) finishes.
    pendingHistoryRefreshRef.current = true;
    resumeLastSession();
  }, [resumeLastSession]);

  const { connected, send } = useWebSocket({ onMessage: onServerMessage, onReconnect, onConnect });

  // Keep sendRef up-to-date (send is stable, but just to be safe)
  useEffect(() => {
    sendRef.current = send;
  }, [send]);

  const [sidebarOpen, setSidebarOpen] = useState(false);

  // Search state
  const [searchOpen, setSearchOpen] = useState(false);
  const [searchQuery, setSearchQuery] = useState("");
  const [searchIndex, setSearchIndex] = useState(0);
  const [matchCount, setMatchCount] = useState(0);
  const searchInputRef = useRef<HTMLInputElement>(null);

  // Update match count after each render when search is active
  useLayoutEffect(() => {
    if (!searchQuery) {
      setMatchCount(0);
      setSearchIndex(0);
      return;
    }
    const matches = document.querySelectorAll(".search-match");
    setMatchCount(matches.length);
    // Reset to first match if count changed
    setSearchIndex((prev) => (matches.length === 0 ? 0 : Math.min(prev, matches.length - 1)));
  });

  // Focus input when search opens
  useEffect(() => {
    if (searchOpen) {
      searchInputRef.current?.focus();
    }
  }, [searchOpen]);

  const closeSearch = useCallback(() => {
    setSearchOpen(false);
    setSearchQuery("");
    setSearchIndex(0);
  }, []);

  // Close search on Escape
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape" && searchOpen) closeSearch();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [searchOpen, closeSearch]);

  const navigateMatch = useCallback((dir: 1 | -1) => {
    const matches = document.querySelectorAll<HTMLElement>(".search-match");
    if (matches.length === 0) return;
    const next = (searchIndex + dir + matches.length) % matches.length;
    setSearchIndex(next);
    matches[next]?.scrollIntoView({ behavior: "smooth", block: "center" });
  }, [searchIndex]);

  const scrollRef = useRef<HTMLDivElement>(null);
  const nearBottomRef = useRef(true);
  // Runs on every message change INCLUDING streaming deltas (the assistant
  // message object grows in place, so messages.length stays constant - we must
  // not gate on length). Follow the bottom only while the user is parked there.
  useEffect(() => {
    const el = scrollRef.current;
    if (!el) return;
    if (loadingOlder) return;
    if (nearBottomRef.current) {
      // Instant (not smooth): rapid deltas would make smooth-scroll fight
      // itself and never catch up. Instant keeps it pinned cleanly.
      el.scrollTop = el.scrollHeight;
    }
  }, [messages, loadingOlder]);

  const onScroll = useCallback(() => {
    const el = scrollRef.current;
    if (!el) return;
    // If the user scrolls up even slightly we stop following so reading isn't
    // interrupted; resume once they come back to the bottom. Small threshold
    // so a tiny nudge counts, but tolerant of sub-pixel rounding.
    nearBottomRef.current = el.scrollHeight - el.scrollTop - el.clientHeight < 48;
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
    (
      text: string,
      autoApproveOnce: boolean,
      images: PromptImage[],
      streamTokens: boolean,
    ) => {
      // In plan mode, prepend an instruction so Claude only plans.
      const promptText = planMode
        ? `[PLAN MODE - read-only, do NOT execute any changes]\n${text}`
        : text;
      appendUserPrompt(text, images);
      const promptMsg: ClientMessage = {
        type: "prompt",
        payload: {
          text: promptText,
          auto_approve: autoApproveOnce,
          stream: streamTokens,
          ...(images.length > 0 ? { images } : {}),
        },
      };
      lastPromptRef.current = promptMsg;
      send(promptMsg);
    },
    [send, appendUserPrompt, planMode]
  );

  const onInterrupt = useCallback(() => {
    send({ type: "interrupt", payload: {} });
  }, [send]);

  // Dynamic /model autocomplete entries sourced from the active worker's
  // models list (hub puts it in `workers` payload). Falls back to nothing
  // when no project is active or the worker hasn't reported models yet.
  const modelCommands = useMemo(() => {
    if (activeProjectId == null) return [];
    const project = projects.find((p) => p.id === activeProjectId);
    if (!project || !project.worker_id) return [];
    const w = workersList.find((wi) => wi.id === project.worker_id);
    if (!w || !w.models || w.models.length === 0) return [];
    return w.models.map((m) => ({
      name: `model ${m.id}`,
      description: `Switch to ${m.name}`,
    }));
  }, [activeProjectId, projects, workersList]);

  const onCommand = useCallback(
    (name: string) => {
      // Dynamic /model <id> handler - matches any model id from the active
      // worker's models list (injected as extraCommands by the Composer).
      if (name.startsWith("model ") && name !== "model default") {
        const modelId = name.slice("model ".length).trim();
        if (modelId) {
          useChatStore.getState().setModel(modelId);
          send({ type: "set_model", payload: { model: modelId } });
          return;
        }
      }
      switch (name) {
        case "clear":
          resetChat();
          break;
        case "compact":
          appendUserPrompt("/compact");
          send({
            type: "prompt",
            payload: {
              text: "Provide a concise summary of our conversation so far: key decisions made, files changed, current state, and any open issues. Be brief - this is to save context window space.",
              stream: true,
            },
          });
          break;
        case "new":
          if (activeProjectId != null) {
            killTerminal();
            resetChat();
            send({ type: "new_session", payload: { project_id: activeProjectId, model: selectedModel } });
          }
          break;
        case "model default":
          useChatStore.getState().setModel(null);
          send({ type: "set_model", payload: { model: null } });
          break;
        case "plan":
          setPlanMode(true);
          break;
        case "act":
          setPlanMode(false);
          break;
      }
    },
    [resetChat, activeProjectId, send, killTerminal, setPlanMode, selectedModel],
  );

  const onNewSession = useCallback(
    (projectId: number) => {
      killTerminal();
      resetChat();
      send({ type: "new_session", payload: { project_id: projectId, model: selectedModel } });
    },
    [resetChat, send, killTerminal, selectedModel]
  );

  const onPickSession = useCallback(
    (projectId: number, sessionId: string) => {
      // If this session is already active - just refresh history, don't
      // restart the session or kill the terminal.
      const alreadyActive = useChatStore.getState().activeSessionId === sessionId;
      if (alreadyActive) {
        send({
          type: "session_history",
          payload: { project_id: projectId, session_id: sessionId, limit: 30 },
        });
        return;
      }
      killTerminal();
      resetChat();
      send({
        type: "session_history",
        payload: { project_id: projectId, session_id: sessionId, limit: 30 },
      });
      send({
        type: "resume_session",
        payload: { project_id: projectId, session_id: sessionId, model: selectedModel },
      });
    },
    [resetChat, send, killTerminal, selectedModel]
  );

  const onTakeover = useCallback(() => {
    if (!pendingLock || pendingLock.projectId === null) return;
    send({
      type: "resume_session",
      payload: {
        project_id: pendingLock.projectId,
        session_id: pendingLock.sessionId,
        force: true,
        model: selectedModel,
      },
    });
    setPendingLock(null);
  }, [pendingLock, send, setPendingLock, selectedModel]);

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

  const onNewProject = useCallback((workerId?: string) => {
    setFsDirectory(null);
    setNewProjectWorkerId(workerId);
    setNewProjectOpen(true);
  }, []);

  const onToggleAutoApprove = useCallback(() => {
    const projectId = activeProjectIdRef.current;
    if (projectId === null) return;
    send({
      type: "set_auto_approve",
      payload: { project_id: projectId, auto_approve: !autoApprove },
    });
  }, [autoApprove, send]);

  return (
    <div className="app-root flex bg-gray-900 text-gray-100 overflow-hidden">
      <Sidebar
        send={send}
        connected={connected}
        open={sidebarOpen}
        onClose={() => setSidebarOpen(false)}
        onPickSession={onPickSession}
        onNewSession={onNewSession}
        onBrowseFiles={onBrowseFiles}
        onNewProject={onNewProject}
      />

      <div className="flex-1 flex flex-col min-w-0 min-h-0">
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
            {messages.length > 0 && (
              <button
                type="button"
                onClick={() => setSearchOpen((v) => !v)}
                className={`p-1 rounded border transition-colors ${
                  searchOpen
                    ? "bg-gray-700 text-gray-200 border-gray-600"
                    : "bg-gray-800 text-gray-400 border-gray-700 hover:bg-gray-700"
                }`}
                title="Search messages"
                aria-label="Search messages"
              >
                <svg width="16" height="16" viewBox="0 0 20 20" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                  <circle cx="8.5" cy="8.5" r="5.5" />
                  <line x1="13" y1="13" x2="18" y2="18" />
                </svg>
              </button>
            )}
            {(activeProjectId !== null || activeSessionId !== null) && (
              <button
                type="button"
                onClick={() => {
                  if (terminalMounted) {
                    setTerminalOpen((v) => !v);
                  } else {
                    setTerminalMounted(true);
                    setTerminalOpen(true);
                  }
                }}
                className={`text-xs px-2 py-1 rounded border transition-colors font-mono ${
                  terminalOpen
                    ? "bg-gray-700 text-gray-200 border-gray-600"
                    : "bg-gray-800 text-gray-400 border-gray-700 hover:bg-gray-700"
                }`}
                title="Toggle terminal"
              >
                &gt;_
              </button>
            )}
            {activeSessionId && (
              <div className="flex items-center gap-1.5 text-[10px] text-gray-500">
                <span className="hover:text-gray-300" title="Change with /model">
                  {selectedModel ? selectedModel.replace("claude-", "").split("-202")[0] : "default"}
                </span>
                <span className="text-gray-600">|</span>
                <span className={planMode ? "text-blue-400" : ""} title="Toggle with /plan or /act">
                  {planMode ? "plan" : "act"}
                </span>
                <span className="text-gray-600">|</span>
                <button
                  type="button"
                  onClick={onToggleAutoApprove}
                  className={`hover:text-gray-300 transition-colors ${autoApprove ? "text-yellow-400" : ""}`}
                  title={autoApprove ? "Auto-approve ON - click to disable" : "Click to auto-approve every tool"}
                >
                  auto {autoApprove ? "on" : "off"}
                </button>
              </div>
            )}
            {activeSessionId && (
              <span className="font-mono hidden md:inline" title={activeSessionId}>
                {activeSessionId.slice(0, 8)}
              </span>
            )}
          </div>
        </header>

        {searchOpen && (
          <div className="border-b border-gray-800 px-3 py-2 flex items-center gap-2 bg-gray-900">
            <svg width="15" height="15" viewBox="0 0 20 20" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" className="text-gray-400 shrink-0">
              <circle cx="8.5" cy="8.5" r="5.5" />
              <line x1="13" y1="13" x2="18" y2="18" />
            </svg>
            <input
              ref={searchInputRef}
              type="text"
              value={searchQuery}
              onChange={(e) => { setSearchQuery(e.target.value); setSearchIndex(0); }}
              placeholder="Search messages…"
              className="flex-1 bg-transparent text-sm text-gray-100 placeholder-gray-500 outline-none"
            />
            {searchQuery && (
              <span className="text-xs text-gray-400 shrink-0 whitespace-nowrap">
                {matchCount === 0 ? "no matches" : `${searchIndex + 1} / ${matchCount}`}
              </span>
            )}
            {searchQuery && matchCount > 0 && (
              <>
                <button
                  type="button"
                  onClick={() => navigateMatch(-1)}
                  className="p-0.5 text-gray-400 hover:text-gray-200 transition-colors"
                  aria-label="Previous match"
                >
                  <svg width="14" height="14" viewBox="0 0 20 20" fill="currentColor">
                    <path d="M10 5l-7 7h14z" />
                  </svg>
                </button>
                <button
                  type="button"
                  onClick={() => navigateMatch(1)}
                  className="p-0.5 text-gray-400 hover:text-gray-200 transition-colors"
                  aria-label="Next match"
                >
                  <svg width="14" height="14" viewBox="0 0 20 20" fill="currentColor">
                    <path d="M10 15l7-7H3z" />
                  </svg>
                </button>
              </>
            )}
            <button
              type="button"
              onClick={closeSearch}
              className="p-0.5 text-gray-400 hover:text-gray-200 transition-colors"
              aria-label="Close search"
            >
              <svg width="14" height="14" viewBox="0 0 20 20" fill="currentColor">
                <path d="M4.293 4.293a1 1 0 011.414 0L10 8.586l4.293-4.293a1 1 0 111.414 1.414L11.414 10l4.293 4.293a1 1 0 01-1.414 1.414L10 11.414l-4.293 4.293a1 1 0 01-1.414-1.414L8.586 10 4.293 5.707a1 1 0 010-1.414z" />
              </svg>
            </button>
          </div>
        )}

        <main
          ref={scrollRef}
          onScroll={onScroll}
          className={`flex-1 overflow-y-auto px-3 md:px-6 py-4${terminalOpen ? " hidden" : ""}`}
        >
          <div className="max-w-3xl lg:max-w-5xl mx-auto">
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
              <Message key={m.id} message={m} searchQuery={searchQuery} />
            ))}
            {status === "streaming" && !currentAssistantId && <TypingIndicator />}
            {lastError && (
              <div className="text-xs text-red-400 mt-2 px-3 py-2 rounded bg-red-950/40 border border-red-800">
                {lastError}
              </div>
            )}
          </div>
        </main>

        {terminalMounted && (activeProjectId ?? activeProjectIdRef.current) !== null && (
          <div className={terminalOpen ? "flex-1 min-h-0" : "hidden"}>
            <Terminal
              key={activeProjectId ?? activeProjectIdRef.current}
              projectId={(activeProjectId ?? activeProjectIdRef.current)!}
              send={send}
              onReady={(write) => { terminalWriteRef.current = write; }}
              onHide={() => setTerminalOpen(false)}
            />
          </div>
        )}

        {pendingUserInputs.map((p) => (
          <UserInputPrompt
            key={p.toolUseId}
            request={p}
            send={send}
            minimized={minimizedInputs.has(p.toolUseId)}
            onToggleMinimize={() => toggleInputMinimized(p.toolUseId)}
            onAnswered={(toolUseId, _answers) => {
              removeUserInput(toolUseId);
            }}
          />
        ))}

        {pendingPermissions.map((p) => (
          <PermissionPrompt
            key={p.toolUseId}
            request={p}
            onAllow={onAllowTool}
            onDeny={onDenyTool}
          />
        ))}

        {/* Composer is hidden while an AskUserQuestion panel is expanded -
            the agent is blocked on the user's answer and the panel needs the
            screen real estate. Minimize the panel to bring the composer back. */}
        <div className={terminalOpen || anyUserInputExpanded ? "hidden" : ""}>
          {streamingActivity && <StreamingStatus activity={streamingActivity} />}
          <Composer
            disabled={!connected || !activeSessionId || readOnly}
            streaming={status === "streaming"}
            autoApproveActive={autoApprove}
            onCommand={onCommand}
            onSubmit={onSubmit}
            onInterrupt={onInterrupt}
            extraCommands={modelCommands}
          />
        </div>
      </div>

      {pendingLock && (
        <LockTakeoverModal
          lock={pendingLock}
          onTakeover={onTakeover}
          onCancel={() => setPendingLock(null)}
        />
      )}

      <FileBrowser send={send} />

      <NewProjectBrowser
        send={send}
        directory={fsDirectory}
        open={newProjectOpen}
        workerId={newProjectWorkerId}
        onClose={() => {
          setNewProjectOpen(false);
          setFsDirectory(null);
          setNewProjectWorkerId(undefined);
        }}
      />
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
