// Service Worker: macht Morgenblatt installierbar und offline lesbar.
// Seite + Ausgabe: Netz zuerst (immer frisch), offline aus dem Cache.
// Schriften, Tailwind, Icons: Cache zuerst. Artikelbilder & Audio gehen direkt ans Netz.
const CACHE = 'morgenblatt-v2';
const SHELL = ['./', 'index.html', 'edition.json', 'manifest.webmanifest', 'icons/icon.svg', 'icons/icon-192.png'];
const STATIC_HOSTS = ['fonts.googleapis.com', 'fonts.gstatic.com', 'cdn.tailwindcss.com'];

self.addEventListener('install', event => {
  event.waitUntil(caches.open(CACHE).then(cache => cache.addAll(SHELL)).then(() => self.skipWaiting()));
});

self.addEventListener('activate', event => {
  event.waitUntil(
    caches.keys()
      .then(keys => Promise.all(keys.filter(k => k !== CACHE).map(k => caches.delete(k))))
      .then(() => self.clients.claim())
  );
});

self.addEventListener('fetch', event => {
  const req = event.request;
  if (req.method !== 'GET') return;
  const url = new URL(req.url);
  const sameOrigin = url.origin === self.location.origin;

  if (sameOrigin && (req.mode === 'navigate' || url.pathname.endsWith('.json') || url.pathname.endsWith('.html'))) {
    event.respondWith(
      fetch(req)
        .then(res => {
          if (res.ok) {
            const copy = res.clone();
            caches.open(CACHE).then(cache => cache.put(req.mode === 'navigate' ? './' : url.pathname, copy));
          }
          return res;
        })
        .catch(() => caches.match(req.mode === 'navigate' ? './' : url.pathname))
    );
    return;
  }

  if (sameOrigin || STATIC_HOSTS.includes(url.hostname)) {
    event.respondWith(
      caches.match(req).then(hit => hit || fetch(req).then(res => {
        if (res.ok || res.type === 'opaque') {
          const copy = res.clone();
          caches.open(CACHE).then(cache => cache.put(req, copy));
        }
        return res;
      }))
    );
  }
});
