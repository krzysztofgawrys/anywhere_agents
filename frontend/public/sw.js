/* Service Worker — handles Web Push notifications for Claude Web */

self.addEventListener('install', () => {
  self.skipWaiting();
});

self.addEventListener('activate', (event) => {
  event.waitUntil(self.clients.claim());
});

// Minimal fetch handler — required for Chrome to treat this as a proper PWA
// and use manifest icons (instead of a generated letter) for Add to Home Screen.
self.addEventListener('fetch', (event) => {
  event.respondWith(fetch(event.request));
});

self.addEventListener('push', (event) => {
  let title = 'Claude';
  let body = '';
  let icon = '/icon-192-v2.png';

  try {
    const data = event.data ? event.data.json() : {};
    title = data.title || title;
    body = data.body || body;
  } catch {
    body = event.data ? event.data.text() : '';
  }

  const options = {
    body,
    icon,
    badge: icon,
    vibrate: [200, 100, 200],
    tag: 'claude-result',
    renotify: true,
    requireInteraction: false,
  };

  // Suppress the notification when the PWA is already on screen — the
  // worker now emits push_notify on every result, so without this check
  // the user gets a notification while staring at the app. If any client
  // window reports visibilityState === 'visible', skip the banner.
  event.waitUntil((async () => {
    const clients = await self.clients.matchAll({
      type: 'window',
      includeUncontrolled: true,
    });
    const hasVisibleClient = clients.some((c) => c.visibilityState === 'visible');
    if (hasVisibleClient) return;
    await self.registration.showNotification(title, options);
  })());
});

self.addEventListener('notificationclick', (event) => {
  event.notification.close();
  event.waitUntil(
    self.clients.matchAll({ type: 'window', includeUncontrolled: true }).then((clientList) => {
      for (const client of clientList) {
        if ('focus' in client) {
          return client.focus();
        }
      }
      if (self.clients.openWindow) {
        return self.clients.openWindow('/');
      }
    })
  );
});
