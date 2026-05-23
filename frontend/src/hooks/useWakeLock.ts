import { useEffect } from "react";

/**
 * Requests a Screen Wake Lock while `active` is true.
 * Prevents the screen from turning off while Claude is working.
 * Silently no-ops on browsers that don't support the API.
 */
export function useWakeLock(active: boolean): void {
  useEffect(() => {
    if (!active) return;
    if (!("wakeLock" in navigator)) return;

    let lock: WakeLockSentinel | null = null;
    let released = false;

    const acquire = (): void => {
      if (released) return;
      navigator.wakeLock
        .request("screen")
        .then((sentinel) => {
          if (released) {
            sentinel.release().catch(() => {});
            return;
          }
          lock = sentinel;
        })
        .catch(() => {
          // Permission denied or API unavailable — fail silently.
        });
    };

    // The Wake Lock API auto-releases when the page becomes hidden.
    // Re-acquire it when the page becomes visible again.
    const onVisibilityChange = (): void => {
      if (document.visibilityState === "visible") {
        acquire();
      }
    };

    acquire();
    document.addEventListener("visibilitychange", onVisibilityChange);

    return () => {
      released = true;
      document.removeEventListener("visibilitychange", onVisibilityChange);
      lock?.release().catch(() => {});
    };
  }, [active]);
}
