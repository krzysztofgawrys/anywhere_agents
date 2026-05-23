import { useCallback, useEffect, useRef, useState } from "react";

const VAPID_KEY_ENDPOINT = "/api/push/vapid-public-key";
const SUBSCRIBE_ENDPOINT = "/api/push/subscribe";

/** Convert a URL-safe base64 VAPID public key to an ArrayBuffer for the PushManager. */
function urlBase64ToArrayBuffer(base64String: string): ArrayBuffer {
  const padding = "=".repeat((4 - (base64String.length % 4)) % 4);
  const base64 = (base64String + padding).replace(/-/g, "+").replace(/_/g, "/");
  const rawData = atob(base64);
  const arr = new Uint8Array(rawData.length);
  for (let i = 0; i < rawData.length; i++) {
    arr[i] = rawData.charCodeAt(i);
  }
  return arr.buffer as ArrayBuffer;
}

async function subscribeWithRegistration(
  reg: ServiceWorkerRegistration
): Promise<void> {
  const keyResp = await fetch(VAPID_KEY_ENDPOINT);
  if (!keyResp.ok) return;
  const { publicKey } = (await keyResp.json()) as { publicKey: string };
  const appServerKey = urlBase64ToArrayBuffer(publicKey);

  let sub = await reg.pushManager.getSubscription();
  if (!sub) {
    try {
      sub = await reg.pushManager.subscribe({ userVisibleOnly: true, applicationServerKey: appServerKey });
    } catch {
      // VAPID key mismatch — unsubscribe stale and retry once
      const stale = await reg.pushManager.getSubscription();
      if (stale) await stale.unsubscribe();
      sub = await reg.pushManager.subscribe({ userVisibleOnly: true, applicationServerKey: appServerKey });
    }
  }

  await fetch(SUBSCRIBE_ENDPOINT, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(sub.toJSON()),
  });
}

/**
 * Manages Web Push subscriptions.
 *
 * On mount: silently registers /sw.js and, if permission is already granted,
 * completes the push subscription (covers page reloads after first consent).
 *
 * Returns `requestSubscription()` — call this on user-meaningful events (e.g.
 * the first `result` message) so the browser permission prompt appears in
 * context, not as a cold popup on page load.
 */
export function usePushNotifications(): {
  requestSubscription: () => void;
  hasPushSubscription: boolean;
} {
  const swRegRef = useRef<ServiceWorkerRegistration | null>(null);
  // Prevent concurrent subscribe calls.
  const subscribingRef = useRef(false);
  const [hasPushSubscription, setHasPushSubscription] = useState(false);

  // Step 1: register SW silently on mount. If already permitted, also subscribe.
  useEffect(() => {
    if (!("serviceWorker" in navigator) || !("PushManager" in window)) return;

    let cancelled = false;
    (async () => {
      try {
        const reg = await navigator.serviceWorker.register("/sw.js");
        if (cancelled) return;
        swRegRef.current = reg;

        // If the user already granted permission (e.g. after a reload),
        // complete the subscription without prompting again.
        if (Notification.permission === "granted") {
          await subscribeWithRegistration(reg);
          if (!cancelled) setHasPushSubscription(true);
        }
      } catch (err) {
        console.warn("[push] SW registration failed:", err);
      }
    })();
    return () => { cancelled = true; };
  }, []);

  // Step 2: exported — call this from a user-meaningful moment.
  const requestSubscription = useCallback(() => {
    if (!("serviceWorker" in navigator) || !("PushManager" in window)) return;
    if (Notification.permission === "denied") return;
    if (subscribingRef.current) return;

    subscribingRef.current = true;
    (async () => {
      try {
        const permission = await Notification.requestPermission();
        if (permission !== "granted") return;

        const reg =
          swRegRef.current ?? (await navigator.serviceWorker.ready);
        await subscribeWithRegistration(reg);
        setHasPushSubscription(true);
      } catch (err) {
        console.warn("[push] subscribe failed:", err);
      } finally {
        subscribingRef.current = false;
      }
    })();
  }, []);

  return { requestSubscription, hasPushSubscription };
}
