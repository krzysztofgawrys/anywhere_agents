import { useCallback, useEffect, useRef } from "react";

/**
 * Returns a function that, when called, shows a local browser notification
 * IF the page is currently hidden (other tab, minimised, phone locked, etc.).
 *
 * Uses the SW's showNotification() so the notification appears even when the
 * page is suspended. Falls back silently if the browser doesn't support it.
 *
 * No backend round-trip needed - this is purely client-side via Page
 * Visibility API + Service Worker.
 */
export function useVisibilityNotify(): (title: string, body: string) => void {
  const swRegRef = useRef<ServiceWorkerRegistration | null>(null);

  // Cache the SW registration once it's ready.
  useEffect(() => {
    if (!("serviceWorker" in navigator)) return;
    navigator.serviceWorker.ready
      .then((reg) => { swRegRef.current = reg; })
      .catch(() => {});
  }, []);

  return useCallback((title: string, body: string) => {
    if (!document.hidden) return; // user can see the result - no notification needed
    if (Notification.permission !== "granted") return;

    playNotificationSound();

    const reg = swRegRef.current;
    if (reg) {
      // Preferred: show via SW so it appears even on suspended pages.
      reg.showNotification(title, {
        body,
        icon: "/favicon.ico",
        badge: "/favicon.ico",
        tag: "claude-result",
        renotify: true,
      } as NotificationOptions).catch(() => {});
    } else if (typeof Notification !== "undefined") {
      // Fallback: plain Notification API.
      new Notification(title, { body, icon: "/favicon.ico" });
    }
  }, []);
}

/**
 * Synthesizes a short two-tone chime using the Web Audio API.
 * Plays 880 Hz then 1100 Hz, each for 120 ms, with a gentle gain envelope.
 * Silently no-ops if the browser does not support AudioContext.
 */
function playNotificationSound(): void {
  try {
    const AudioCtxCtor =
      window.AudioContext ??
      (window as Window & { webkitAudioContext?: typeof AudioContext })
        .webkitAudioContext;
    if (!AudioCtxCtor) return;

    const ctx = new AudioCtxCtor();

    const playTone = (frequency: number, startTime: number): void => {
      const osc = ctx.createOscillator();
      const gain = ctx.createGain();

      osc.type = "sine";
      osc.frequency.setValueAtTime(frequency, startTime);

      // Gentle envelope: quick attack, hold briefly, then fade
      gain.gain.setValueAtTime(0, startTime);
      gain.gain.linearRampToValueAtTime(0.25, startTime + 0.01);
      gain.gain.setValueAtTime(0.25, startTime + 0.08);
      gain.gain.linearRampToValueAtTime(0, startTime + 0.12);

      osc.connect(gain);
      gain.connect(ctx.destination);

      osc.start(startTime);
      osc.stop(startTime + 0.12);
    };

    const now = ctx.currentTime;
    playTone(880, now);          // first tone: 880 Hz
    playTone(1100, now + 0.15);  // second tone: 1100 Hz, 150 ms later

    // Close the context once both tones have finished.
    setTimeout(() => { ctx.close().catch(() => {}); }, 400);
  } catch {
    // Web Audio not supported or blocked - fail silently.
  }
}
