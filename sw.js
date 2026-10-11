// BoothWatch service worker: shows phone notifications and opens the dashboard when you tap one.
self.addEventListener('install', () => self.skipWaiting());
self.addEventListener('activate', e => e.waitUntil(self.clients.claim()));

self.addEventListener('push', event => {
  let d = {};
  try { d = event.data ? event.data.json() : {}; } catch (e) { d = { title: 'BoothWatch', body: event.data ? event.data.text() : '' }; }
  event.waitUntil(self.registration.showNotification(d.title || 'BoothWatch', {
    body: d.body || '',
    tag: d.tag || undefined,
    icon: 'icon-192.png',
    badge: 'badge.png',
    data: { url: d.url || './', booth_id: d.booth_id || null },
  }));
});

self.addEventListener('notificationclick', event => {
  event.notification.close();
  const data = event.notification.data || {};
  event.waitUntil((async () => {
    const isSupport = /^Support/.test(event.notification.title || '');
    const all = await self.clients.matchAll({ type: 'window', includeUncontrolled: true });
    for (const c of all) {
      if ('focus' in c) {
        await c.focus();
        c.postMessage(isSupport ? { type: 'open-page', page: 'support' } : { type: 'open-booth', booth_id: data.booth_id });
        return;
      }
    }
    await self.clients.openWindow((data.url || './').split('#')[0] + (isSupport ? '#/support' : ''));
  })());
});
