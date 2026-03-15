// katalog.ai Service Worker
const CACHE_NAME = 'katalog-v1';
const STATIC_ASSETS = [
  '/',
  '/static/index.html',
  'https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&display=swap',
  'https://cdnjs.cloudflare.com/ajax/libs/jsbarcode/3.11.6/JsBarcode.all.min.js',
];

// ── INSTALL — cache static assets ──
self.addEventListener('install', e => {
  e.waitUntil(
    caches.open(CACHE_NAME).then(cache => {
      return cache.addAll(STATIC_ASSETS).catch(err => {
        console.warn('SW: some assets failed to cache', err);
      });
    })
  );
  self.skipWaiting();
});

// ── ACTIVATE — clean old caches ──
self.addEventListener('activate', e => {
  e.waitUntil(
    caches.keys().then(keys =>
      Promise.all(keys.filter(k => k !== CACHE_NAME).map(k => caches.delete(k)))
    )
  );
  self.clients.claim();
});

// ── FETCH — network first, cache fallback ──
self.addEventListener('fetch', e => {
  const url = new URL(e.request.url);

  // Always network for API calls
  if (url.pathname.startsWith('/api/') || url.hostname.includes('supabase')) {
    return;
  }

  // Cache first for images
  if (e.request.destination === 'image') {
    e.respondWith(
      caches.match(e.request).then(cached => {
        if (cached) return cached;
        return fetch(e.request).then(res => {
          if (res.ok) {
            const clone = res.clone();
            caches.open(CACHE_NAME).then(cache => cache.put(e.request, clone));
          }
          return res;
        }).catch(() => cached);
      })
    );
    return;
  }

  // Network first for HTML/JS/CSS, fallback to cache
  e.respondWith(
    fetch(e.request)
      .then(res => {
        if (res.ok && e.request.method === 'GET') {
          const clone = res.clone();
          caches.open(CACHE_NAME).then(cache => cache.put(e.request, clone));
        }
        return res;
      })
      .catch(() => caches.match(e.request))
  );
});

// ── PUSH NOTIFICATIONS ──
self.addEventListener('push', e => {
  const data = e.data ? e.data.json() : {};
  const title   = data.title   || 'katalog.ai';
  const body    = data.body    || 'Novi katalog je dostupan!';
  const icon    = data.icon    || '/static/icon-192.png';
  const badge   = data.badge   || '/static/icon-192.png';
  const url     = data.url     || '/';

  e.waitUntil(
    self.registration.showNotification(title, {
      body,
      icon,
      badge,
      vibrate: [200, 100, 200],
      data: { url },
      actions: [
        { action: 'open',    title: '📖 Otvori' },
        { action: 'dismiss', title: 'Zatvori'   },
      ],
    })
  );
});

self.addEventListener('notificationclick', e => {
  e.notification.close();
  if (e.action === 'dismiss') return;
  const url = e.notification.data?.url || '/';
  e.waitUntil(
    clients.matchAll({ type: 'window', includeUncontrolled: true }).then(clientList => {
      for (const client of clientList) {
        if (client.url === url && 'focus' in client) return client.focus();
      }
      if (clients.openWindow) return clients.openWindow(url);
    })
  );
});
