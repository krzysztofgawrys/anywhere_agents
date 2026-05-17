import { useCallback, useEffect, useRef } from "react";
import { Composer } from "./components/Composer";
import { Message } from "./components/Message";
import { useWebSocket } from "./hooks/useWebSocket";
import { useChatStore } from "./stores/chat";

function App() {
  const messages = useChatStore((s) => s.messages);
  const status = useChatStore((s) => s.status);
  const lastError = useChatStore((s) => s.lastError);
  const cost = useChatStore((s) => s.cost);
  const handleServerMessage = useChatStore((s) => s.handleServerMessage);
  const appendUserPrompt = useChatStore((s) => s.appendUserPrompt);

  const onServerMessage = useCallback(handleServerMessage, [handleServerMessage]);
  const { connected, send } = useWebSocket({ onMessage: onServerMessage });

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

  return (
    <div className="h-screen flex flex-col bg-gray-900 text-gray-100">
      <header className="border-b border-gray-700 px-4 py-2 md:py-3 flex items-center justify-between">
        <div className="flex items-center gap-3">
          <h1 className="text-lg md:text-xl font-semibold">Claude Web</h1>
          <div className="flex items-center gap-1.5">
            <div
              className={`w-2 h-2 rounded-full ${
                connected ? "bg-green-400" : "bg-red-400"
              }`}
            />
            <span className="text-xs text-gray-400">
              {connected ? "connected" : "disconnected"}
            </span>
          </div>
        </div>
        <div className="text-xs text-gray-500">
          {cost > 0 && <span>${cost.toFixed(4)}</span>}
        </div>
      </header>

      <main
        ref={scrollRef}
        className="flex-1 overflow-y-auto px-3 md:px-6 py-4"
      >
        <div className="max-w-3xl mx-auto">
          {messages.length === 0 && (
            <div className="text-center text-gray-500 mt-12">
              <p className="text-sm md:text-base">
                Phase 2 — single hardcoded session
              </p>
              <p className="text-xs mt-2">Type a message below to start.</p>
            </div>
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
        disabled={!connected}
        streaming={status === "streaming"}
        onSubmit={onSubmit}
        onInterrupt={onInterrupt}
      />
    </div>
  );
}

export default App;
