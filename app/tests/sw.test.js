/**
 * sw.test.js — the service worker, which had never been tested.
 *
 * WHY THIS FILE EXISTS. This worker is the layer that held the staleness in
 * place for weeks: it is network-first, which reads like it cannot serve an old
 * page, and its fetch() went through the very HTTP cache that was answering
 * stale. Nothing here was covered, so the pre-push gate was the first reader
 * this file ever had — and it found that ANY response was cached, including a
 * 4xx/5xx.
 *
 * That is not a theoretical hole. The reload button asks the server for the
 * shell and, on a bad answer, promises that nothing was changed and the copy in
 * hand still works. Cache an error page under `/` on the way past and that
 * promise is false: the next dead zone serves the error instead of the app.
 *
 * The worker runs in no DOM, so this drives it in a vm context with `self`,
 * `caches` and `fetch` stubbed, and asserts on what ends up in the store.
 *
 * Run: cd app && npm install && npm test
 */
const fs = require("fs");
const path = require("path");
const vm = require("vm");

const SW = fs.readFileSync(path.join(__dirname, "..", "sw.js"), "utf8");

let passed = 0, failed = 0;
const check = (name, cond, detail = "") => {
  if (cond) { passed++; console.log(`[PASS] ${name}`); }
  else { failed++; console.log(`[FAIL] ${name}  ${JSON.stringify(detail)}`); }
};
const drain = () => new Promise(r => setTimeout(r, 0));

/** A response the worker will accept as real. `body` is what a cache hit
 *  would later hand back, so the tests can tell the copies apart. */
function res(body, { ok = true, status = 200 } = {}) {
  return { ok, status, body, clone() { return res(body, { ok, status }); } };
}

/**
 * Boot sw.js against a fake Cache Storage.
 * `netFor(url)` decides what the network does for each request.
 */
function loadSW(netFor) {
  const listeners = {};
  const store = new Map();              // cacheName -> Map(key -> response)
  const cacheFor = (name) => {
    if (!store.has(name)) store.set(name, new Map());
    return store.get(name);
  };
  const waited = [], asked = [];

  const ctx = {
    self: {
      addEventListener: (type, fn) => { listeners[type] = fn; },
      skipWaiting: () => {},
      clients: { claim: () => {} },
    },
    URL,
    Request: class { constructor(url, init) { this.url = url; this.headers = (init || {}).headers; } },
    Promise, setTimeout, console,
    fetch: (req, init) => {
      asked.push({ url: typeof req === "string" ? req : req.url, init: init || {} });
      const out = netFor(typeof req === "string" ? req : req.url);
      return out ? Promise.resolve(out) : Promise.reject(new TypeError("Failed to fetch"));
    },
    caches: {
      open: async (name) => ({
        addAll: async (urls) => urls.forEach(u => cacheFor(name).set("https://s.ts.net" + u, res("shell:" + u))),
        put: async (req, r) => cacheFor(name).set(typeof req === "string" ? req : req.url, r),
      }),
      keys: async () => [...store.keys()],
      delete: async (name) => store.delete(name),
      match: async (req, opts) => {
        const url = new URL(typeof req === "string" ? req : req.url);
        const want = (opts && opts.ignoreSearch) ? url.origin + url.pathname : url.href;
        for (const c of store.values()) {
          for (const [k, v] of c) {
            const kk = new URL(k);
            if (((opts && opts.ignoreSearch) ? kk.origin + kk.pathname : kk.href) === want) return v;
          }
        }
        return undefined;
      },
    },
  };
  vm.createContext(ctx);
  vm.runInContext(SW, ctx);

  /** Drive one fetch event; resolves to what the worker answered with. */
  const fire = (url) => {
    let answered;
    const e = {
      request: { url, method: "GET", headers: {} },
      respondWith: (p) => { answered = p; },
      waitUntil: (p) => { waited.push(p); },
    };
    listeners.fetch(e);
    return answered;
  };

  return { listeners, store, fire, waited, asked, cacheFor,
           installed: () => listeners.install({ waitUntil: (p) => waited.push(p) }) };
}

const CACHE = "thincart-shell-v9";
const ORIGIN = "https://s.ts.net";

(async () => {
  console.log("\n--- 1. a good response updates the shell -------------------------");
  {
    const sw = loadSW(() => res("fresh page"));
    const out = await sw.fire(ORIGIN + "/");
    await drain(); await Promise.all(sw.waited);
    check("the page was answered from the network", out.body === "fresh page", out);
    check("and stored", sw.cacheFor(CACHE).get(ORIGIN + "/").body === "fresh page",
      [...sw.cacheFor(CACHE).keys()]);
    check("the write was held open with waitUntil", sw.waited.length === 1, sw.waited.length);
    /* The whole point of network-first, and the thing it did NOT do before: a
     * plain fetch() consults the HTTP cache first, so a heuristically-fresh
     * entry answered ahead of the network and the worker never noticed. That is
     * the mechanism that made a deploy and a phone disagree for weeks, and it
     * lived here as much as in the missing response headers. */
    check("the network was genuinely asked, not the cache",
      sw.asked[0] && sw.asked[0].init.cache === "no-cache", sw.asked[0]);
  }

  console.log("\n--- 2. an error response NEVER replaces the working shell ---------");
  {
    /* The gate's finding, and the reason this file exists. A 5xx is a reply,
     * not a network failure, so it arrives on the success path. Cached, it
     * would overwrite the offline shell with an error page while the reload
     * button was truthfully reporting that nothing had changed. */
    const sw = loadSW(() => res("<h1>502 Bad Gateway</h1>", { ok: false, status: 502 }));
    await sw.installed(); await Promise.all(sw.waited);
    const before = sw.cacheFor(CACHE).get(ORIGIN + "/").body;
    const out = await sw.fire(ORIGIN + "/");
    await drain(); await Promise.all(sw.waited);
    check("the error is still passed through to the page", out.status === 502, out);
    check("but the shell in the cache is untouched",
      sw.cacheFor(CACHE).get(ORIGIN + "/").body === before,
      { before, after: sw.cacheFor(CACHE).get(ORIGIN + "/").body });
    check("so an offline launch still gets the app, not the error",
      before.startsWith("shell:"), before);
  }

  console.log("\n--- 3. a dead network falls back to the cached shell --------------");
  {
    const sw = loadSW(() => null);          // fetch rejects
    await sw.installed(); await Promise.all(sw.waited);
    const out = await sw.fire(ORIGIN + "/");
    check("served from cache", out && out.body === "shell:/", out);
  }

  console.log("\n--- 4. the cache-buster does not accumulate copies ----------------");
  {
    /* The launcher's "Force a fresh copy" uses /?fresh=<ms>, different every
     * time. Stored verbatim, each forced refresh would leave a permanent extra
     * shell that the reads — which ignore the query — never look at again. */
    const sw = loadSW(() => res("fresh page"));
    await sw.fire(ORIGIN + "/?fresh=1");
    await sw.fire(ORIGIN + "/?fresh=2");
    await sw.fire(ORIGIN + "/?fresh=3");
    await drain(); await Promise.all(sw.waited);
    const keys = [...sw.cacheFor(CACHE).keys()];
    check("three forced refreshes leave one entry", keys.length === 1, keys);
    check("stored under the plain path", keys[0] === ORIGIN + "/", keys);
  }

  console.log("\n--- 5. state and ops are never cached -----------------------------");
  {
    const sw = loadSW(() => res("state"));
    check("/api is left to the network", sw.fire(ORIGIN + "/api/state") === undefined);
    check("/ws is left to the network", sw.fire(ORIGIN + "/ws") === undefined);
  }

  console.log("\n--- 6. activate evicts every older shell ---------------------------");
  {
    const sw = loadSW(() => res("fresh page"));
    await sw.installed(); await Promise.all(sw.waited);   // the current cache exists
    sw.store.set("thincart-shell-v7", new Map([[ORIGIN + "/", res("the 2026-09-06 page")]]));
    sw.store.set("thincart-shell-v8", new Map([[ORIGIN + "/", res("yesterday's page")]]));
    const waited = [];
    sw.listeners.activate({ waitUntil: (p) => waited.push(p) });
    await Promise.all(waited);
    check("only the current cache survives", [...sw.store.keys()].join(",") === CACHE,
      [...sw.store.keys()]);
  }

  console.log(`\n================ ${passed} passed, ${failed} failed ================`);
  process.exit(failed === 0 ? 0 : 1);
})();
