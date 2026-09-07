/* ThinCart service worker — app-shell cache ONLY.
 * The offline op queue lives in page JS (localStorage), NOT here:
 * Background Sync is unsupported on iOS, so the page owns queue flushing. */
/* Bumped v7 -> v8 on 2026-09-07 with the no-cache headers: any shell already
 * sitting in Cache Storage from before that fix is evicted on activate. */
const CACHE = 'thincart-shell-v8';
const SHELL = ['/', '/manifest.json', '/icon-192.png', '/icon-512.png'];

self.addEventListener('install', e => {
  e.waitUntil(caches.open(CACHE).then(c => c.addAll(SHELL)).then(() => self.skipWaiting()));
});

self.addEventListener('activate', e => {
  e.waitUntil(
    caches.keys()
      .then(keys => Promise.all(keys.filter(k => k !== CACHE).map(k => caches.delete(k))))
      .then(() => self.clients.claim())
  );
});

self.addEventListener('fetch', e => {
  const url = new URL(e.request.url);
  if (e.request.method !== 'GET' || url.pathname.startsWith('/api') || url.pathname === '/ws')
    return; // network only — never cache state or ops
  // network-first for the shell: updates propagate, dead zones fall back to cache
  e.respondWith(
    fetch(e.request)
      .then(res => {
        const copy = res.clone();
        caches.open(CACHE).then(c => c.put(e.request, copy));
        return res;
      })
      .catch(() => caches.match(e.request, { ignoreSearch: true }))
  );
});
