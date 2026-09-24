/* Kijiji Tanzania / JAMII TANZANIA - Service Worker (v18)
   Offline page always shows when network fails AND the requested page
   isn't already cached. If the requested page (e.g. /home with its
   cached videos) IS cached, it's shown for real instead of the offline card.
   Opening /chat (navigation) also goes through this same fallback now —
   only the live chat data calls stay strictly network-only.
   Bump CACHE_VERSION when you change icons/logo/important assets,
   OR when you change offline.html / this file — otherwise already-installed
   users keep serving the old cached behavior forever, because the browser
   only re-runs "install" when this sw.js file's bytes change.
*/

const CACHE_VERSION = 'jamii-v19';
const CACHE_NAME = `jamii-cache-${CACHE_VERSION}`;
const MEDIA_CACHE = `jamii-media-${CACHE_VERSION}`;
const OFFLINE_VIDEOS_CACHE = 'jamii-offline-videos'; // matches base.html
const MAX_MEDIA = 80;

const OFFLINE_URL = '/offline';

const PRECACHE_URLS = [
  '/',
  '/home',
  OFFLINE_URL,
  '/static/icon-192-v6.png',
  '/static/icon-512-v6.png'
];

/* ========== INSTALL ========== */
self.addEventListener('install', (event) => {
  self.skipWaiting();

  event.waitUntil(
    (async () => {
      const cache = await caches.open(CACHE_NAME);

      // Force-cache the offline page first (most important)
      try {
        const offlineRes = await fetch(OFFLINE_URL, { cache: 'reload' });
        if (offlineRes && offlineRes.ok) {
          await cache.put(OFFLINE_URL, offlineRes.clone());
        }
      } catch (e) {
        console.warn('[SW] offline page precache failed', e);
      }

      // Precache the rest (ignore individual failures)
      await Promise.all(
        PRECACHE_URLS.map(async (url) => {
          if (url === OFFLINE_URL) return;
          try {
            await cache.add(url);
          } catch (err) {
            console.log('[SW] Precache skip', url, err);
          }
        })
      );
    })()
  );
});

/* ========== ACTIVATE ========== */
self.addEventListener('activate', (event) => {
  event.waitUntil(
    (async () => {
      const keys = await caches.keys();
      await Promise.all(
        keys
          .filter((key) => {
            if (!key.startsWith('jamii-')) return false;
            // Keep current shell + media + user offline videos
            if (
              key === CACHE_NAME ||
              key === MEDIA_CACHE ||
              key === OFFLINE_VIDEOS_CACHE
            ) {
              return false;
            }
            return true;
          })
          .map((key) => caches.delete(key))
      );
      await self.clients.claim();
    })()
  );
});

/* ========== MESSAGES ========== */
self.addEventListener('message', (event) => {
  const data = event.data || {};

  if (data.type === 'SKIP_WAITING') {
    self.skipWaiting();
    return;
  }

  if (data.type === 'CACHE_MEDIA' && data.url) {
    event.waitUntil(
      (async () => {
        try {
          const cache = await caches.open(MEDIA_CACHE);
          const res = await fetch(data.url);
          if (res && res.ok) {
            await cache.put(data.url, res.clone());
            await trimMediaCache();
          }
          if (event.ports && event.ports[0]) {
            event.ports[0].postMessage({ ok: true, url: data.url });
          }
        } catch (e) {
          if (event.ports && event.ports[0]) {
            event.ports[0].postMessage({ ok: false, error: String(e) });
          }
        }
      })()
    );
  }
});

/* ========== HELPERS ========== */
async function trimMediaCache() {
  const cache = await caches.open(MEDIA_CACHE);
  const keys = await cache.keys();
  if (keys.length <= MAX_MEDIA) return;
  const removeCount = keys.length - MAX_MEDIA;
  for (let i = 0; i < removeCount; i++) {
    await cache.delete(keys[i]);
  }
}

function isMediaPath(pathname) {
  return (
    pathname.startsWith('/static/uploads/') ||
    /\.(mp4|webm|ogg|mov|m4v|jpg|jpeg|png|gif|webp|mp3|wav|m4a|aac|opus)(\?|$)/i.test(
      pathname
    )
  );
}

function isNetworkOnly(pathname) {
  return (
    pathname.startsWith('/api/') ||
    pathname.startsWith('/call/') ||
    pathname.includes('signals') ||
    pathname.startsWith('/chat') ||
    pathname.startsWith('/status/') ||
    pathname.startsWith('/like/') ||
    pathname.startsWith('/comment') ||
    pathname.startsWith('/kijiji/') ||
    pathname.startsWith('/inbox')
  );
}

async function getOfflinePage() {
  // 1. Designed offline page
  const offline = await caches.match(OFFLINE_URL);
  if (offline) return offline;

  // 2. Emergency minimal page
  return new Response(
    `<!DOCTYPE html>
<html lang="sw">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <meta name="theme-color" content="#1EB53A">
  <title>Uko Offline — Kijiji Tanzania</title>
  <style>
    body{margin:0;min-height:100vh;display:flex;align-items:center;justify-content:center;
      font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;
      background:#06140c;color:#fff;text-align:center;padding:24px}
    h1{font-size:22px;margin:0 0 12px}
    p{color:#9aa99e;line-height:1.5;margin:0 0 24px}
    a{display:inline-block;background:linear-gradient(135deg,#1EB53A,#00A3DD);
      color:#fff;text-decoration:none;padding:12px 24px;border-radius:12px;font-weight:700}
  </style>
</head>
<body>
  <div>
    <h1>📡 Uko Offline</h1>
    <p>Hakuna mtandao sasa.<br>Unaweza kuona kurasa na media zilizohifadhiwa.</p>
    <a href="/">Jaribu Tena</a>
  </div>
</body>
</html>`,
    {
      status: 200,
      headers: { 'Content-Type': 'text/html; charset=utf-8' }
    }
  );
}

/* ========== FETCH ========== */
self.addEventListener('fetch', (event) => {
  const req = event.request;
  const url = new URL(req.url);

  // Only same-origin GET
  if (req.method !== 'GET') return;
  if (url.origin !== self.location.origin) return;

  // Detect navigation (opening a page) up front so we can tell it apart
  // from a background data call to the same path prefix (e.g. /chat).
  const isNavigate =
    req.mode === 'navigate' ||
    (req.headers.get('accept') || '').includes('text/html');

  // Realtime / API data calls → network only (let browser fail naturally).
  // IMPORTANT: this must NOT block opening the page itself — e.g. tapping
  // the Chat button should still open the chat screen (from cache, or the
  // offline fallback) even with no network. Only the live data underneath
  // (messages, signals, sockets) genuinely can't work offline.
  if (isNetworkOnly(url.pathname) && !isNavigate) return;

  // Manifest + icons → network first
  if (
    url.pathname.endsWith('manifest.json') ||
    url.pathname.includes('/icon-') ||
    url.pathname.includes('manifest')
  ) {
    event.respondWith(
      fetch(req)
        .then((res) => {
          if (res && res.status === 200) {
            const clone = res.clone();
            caches.open(CACHE_NAME).then((c) => c.put(req, clone));
          }
          return res;
        })
        .catch(() => caches.match(req))
    );
    return;
  }

  // Media (videos / images / audio)
  if (isMediaPath(url.pathname)) {
    event.respondWith(
      (async () => {
        // 1. User explicit offline downloads
        try {
          const off = await caches.open(OFFLINE_VIDEOS_CACHE);
          const hit = await off.match(req);
          if (hit) return hit;
        } catch (e) {}

        // 2. Automatic media cache
        try {
          const cache = await caches.open(MEDIA_CACHE);
          const hit = await cache.match(req);
          if (hit) return hit;

          try {
            const res = await fetch(req);
            if (res && res.ok) {
              cache.put(req, res.clone()).then(() => trimMediaCache()).catch(() => {});
            }
            return res;
          } catch (e) {
            return (
              (await cache.match(req)) ||
              (await caches.match(req)) ||
              new Response('', { status: 503, statusText: 'Offline media' })
            );
          }
        } catch (e) {
          return (
            (await caches.match(req)) ||
            new Response('', { status: 503, statusText: 'Offline media' })
          );
        }
      })()
    );
    return;
  }

  // HTML pages / navigations → Network first, then designed offline page
  if (isNavigate) {
    event.respondWith(
      (async () => {
        try {
          const res = await fetch(req);
          if (res && res.status === 200) {
            const clone = res.clone();
            caches.open(CACHE_NAME).then((c) => c.put(req, clone));
          }
          return res;
        } catch (err) {
          // Priority order when offline:
          // 1. The page user asked for (if previously cached) — e.g. /home
          //    with its cached videos should show for real, not the offline card
          // 2. Designed offline page (only if the requested page isn't cached)
          // 3. /home
          // 4. /
          // 5. Emergency HTML
          return (
            (await caches.match(req)) ||
            (await caches.match(OFFLINE_URL)) ||
            (await caches.match('/home')) ||
            (await caches.match('/')) ||
            (await getOfflinePage())
          );
        }
      })()
    );
    return;
  }

  // Other static assets → cache first
  event.respondWith(
    caches.match(req).then((cached) => {
      if (cached) return cached;
      return fetch(req)
        .then((res) => {
          if (res && res.status === 200) {
            const clone = res.clone();
            caches.open(CACHE_NAME).then((c) => c.put(req, clone));
          }
          return res;
        })
        .catch(() => cached);
    })
  );
});

/* ========== PUSH ========== */
self.addEventListener('push', function (event) {
  var data = {
    title: 'JAMII TANZANIA',
    body: 'Taarifa mpya',
    url: '/notifications',
    icon: '/static/icon-192-v6.png',
    badge: '/static/icon-192-v6.png'
  };

  try {
    if (event.data) {
      var parsed = event.data.json();
      data.title = parsed.title || data.title;
      data.body = parsed.body || data.body;
      data.url = parsed.url || data.url;
      data.icon = parsed.icon || data.icon;
      data.badge = parsed.badge || data.icon;
    }
  } catch (e) {
    console.log('Push parse error', e);
  }

  event.waitUntil(
    self.registration.showNotification(data.title, {
      body: data.body,
      icon: data.icon,
      badge: data.badge,
      data: { url: data.url },
      vibrate: [120, 60, 120],
      renotify: true,
      tag: 'kijiji-notif-' + Date.now(),
      requireInteraction: false,
      actions: [
        { action: 'open', title: 'Fungua' },
        { action: 'close', title: 'Funga' }
      ]
    })
  );
});

/* ========== NOTIFICATION CLICK ========== */
self.addEventListener('notificationclick', function (event) {
  event.notification.close();
  if (event.action === 'close') return;

  var url = '/notifications';
  if (event.notification.data && event.notification.data.url) {
    url = event.notification.data.url;
  }

  event.waitUntil(
    clients
      .matchAll({ type: 'window', includeUncontrolled: true })
      .then(function (clientList) {
        for (var i = 0; i < clientList.length; i++) {
          var client = clientList[i];
          if (client.url && 'focus' in client) {
            client.navigate(url);
            return client.focus();
          }
        }
        if (clients.openWindow) return clients.openWindow(url);
      })
  );
});
