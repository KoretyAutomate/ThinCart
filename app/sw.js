/* ThinCart service worker — app-shell cache ONLY.
 * The offline op queue lives in page JS (localStorage), NOT here:
 * Background Sync is unsupported on iOS, so the page owns queue flushing. */
/* Bumped v7 -> v8 on 2026-09-07 with the no-cache headers: any shell already
 * sitting in Cache Storage from before that fix is evicted on activate.
 * v8 -> v9 later the same day, when the shell learned to be cached under a
 * query-stripped key (see below) — the old entries key differently and would
 * otherwise be dead weight that still answers. */
const CACHE = 'thincart-shell-v9';
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

/* The key a response is STORED under, with any query string dropped.
 * `?fresh=<ms>` is how the APK launcher forces past a stuck HTTP cache entry,
 * and the value is different every time. Cached verbatim, each forced refresh
 * would leave a permanent extra copy of the shell behind — the reads already
 * ignore the query (see the fallback below), so the writes must too or the
 * store grows without bound and nothing ever reads the extras. */
const cacheKey = req => new Request(new URL(req.url).origin + new URL(req.url).pathname,
                                   { headers: req.headers });

self.addEventListener('fetch', e => {
  const url = new URL(e.request.url);
  if (e.request.method !== 'GET' || url.pathname.startsWith('/api') || url.pathname === '/ws')
    return; // network only — never cache state or ops
  /* Network-first for the shell — and 'no-cache' is what makes that true rather
   * than merely intended. This handler used to call plain fetch(), whose default
   * cache mode consults the HTTP cache first; an entry with heuristic freshness
   * therefore answered before the network was ever asked, and "network-first"
   * was first in line behind a hit. That is the mechanism that made a deploy and
   * a phone disagree for weeks, and it lived HERE, not only in the missing
   * response headers.
   *
   * 'no-cache' means revalidate, not re-download: a conditional request goes out
   * every time and an unchanged page comes back 304 for a few bytes. So the
   * phone is current on every navigation the worker sees, at no meaningful cost,
   * and a dead zone still falls through to the cached shell below. */
  e.respondWith(
    fetch(e.request, { cache: 'no-cache' })
      .then(res => {
        /* ONLY a good response replaces what we are holding. A 4xx/5xx is a
         * reply, not a failure, so it lands here rather than in the catch — and
         * cached, it would overwrite a working offline shell with an error page.
         * The page's reload button makes that reachable on purpose: it asks the
         * server for the shell, and on a bad answer it reports that nothing was
         * changed and keeps the copy in hand. That promise is only true if this
         * has not already thrown the copy away underneath it.
         *
         * waitUntil, because the write outlives the response: without it the
         * worker may be killed the moment the page has its bytes, leaving the
         * shell half-updated for the next dead zone. */
        if (res.ok) {
          const copy = res.clone();
          e.waitUntil(caches.open(CACHE).then(c => c.put(cacheKey(e.request), copy)));
        }
        return res;
      })
      .catch(() => caches.match(e.request, { ignoreSearch: true }))
  );
});
