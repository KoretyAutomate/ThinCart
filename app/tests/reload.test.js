/**
 * reload.test.js — the button that makes a stale phone current.
 *
 * WHY THIS FILE EXISTS. On 2026-09-07 the cause of "I deploy and the phone
 * shows the old app" was found and fixed at the server, and the phone kept
 * showing the old app anyway: the fix only binds copies fetched AFTER it
 * shipped, and the handset was holding one from before. The page's own advice
 * at that moment was "pull down / reopen", and neither does anything — a
 * Capacitor WebView has no pull-to-refresh, and a reopen re-navigates to a URL
 * the cache is still entitled to answer. There was no way out of the app except
 * Android's app-storage screen.
 *
 * So the property under test is not "a button exists". It is the ORDER: the
 * fresh copy is fetched before the offline one is thrown away, and when the
 * fetch fails NOTHING is thrown away. Get that backwards and the button becomes
 * a way to be left in a shop with no app at all.
 *
 * Run: cd app && npm install && npm test
 */
const fs = require("fs");
const path = require("path");
const { JSDOM } = require("jsdom");

const HTML = fs.readFileSync(path.join(__dirname, "..", "index.html"), "utf8");
const SCRIPT = HTML.split("<script>")[1].split("</script>")[0];

let passed = 0, failed = 0;
const check = (name, cond, detail = "") => {
  if (cond) { passed++; console.log(`[PASS] ${name}`); }
  else { failed++; console.log(`[FAIL] ${name}  ${JSON.stringify(detail)}`); }
};
const drain = () => new Promise(r => setTimeout(r, 0));

/**
 * Boot the real page with a stubbed cache layer.
 * `pageOk` decides what GET / does — the one request the button depends on.
 */
function boot({ lang = "en", pageOk = true } = {}) {
  const dom = new JSDOM(HTML, { runScripts: "outside-only", url: "https://spark.example.ts.net/" });
  const w = dom.window;

  w.localStorage.setItem("pc_name", "tester");
  w.localStorage.setItem("pc_base", JSON.stringify(
    { revision: 1, items: [], stores: [], picks: {}, suggestions: [], away_pending: 0 }));
  w.localStorage.setItem("pc_lang", lang);

  const fetches = [];
  w.fetch = (url, opts) => {
    fetches.push({ url: String(url), opts: opts || {} });
    if (String(url) === "/" || String(url) === "/index.html") {
      return pageOk
        ? Promise.resolve({ ok: true, status: 200, text: async () => "<html>fresh</html>" })
        : Promise.reject(new TypeError("Failed to fetch"));
    }
    if (String(url).includes("/health"))
      return Promise.resolve({ ok: true, status: 200, json: async () => ({ build: "deadbeef" }) });
    return Promise.resolve({ ok: false, status: 404, json: async () => ({}), text: async () => "" });
  };

  // Cache Storage and the worker registry, recorded rather than performed.
  // Deletions are recorded too — not because anything should delete, but so a
  // test can prove nothing does.
  const deleted = [], unregistered = [], updated = [];
  w.caches = {
    keys: async () => ["thincart-shell-v7", "thincart-shell-v9"],
    delete: async (k) => { deleted.push(k); return true; },
  };
  Object.defineProperty(w.navigator, "serviceWorker", {
    configurable: true,
    value: { register: () => {},
             getRegistrations: async () => [{
               update: async () => { updated.push(1); return true; },
               unregister: async () => { unregistered.push(1); return true; },
             }] },
  });
  w.WebSocket = function () { this.close = () => {}; };
  w.eval(SCRIPT);

  const reloads = [];
  w.hardReload = () => reloads.push(1);

  return { w, fetches, deleted, unregistered, updated, reloads,
           btn: w.document.getElementById("reload-btn"),
           err: w.document.getElementById("reload-err"),
           line: w.document.getElementById("buildline"),
           panel: w.document.getElementById("set-panel"),
           gear: w.document.getElementById("set-btn") };
}

(async () => {
  console.log("\n--- 0. it is somewhere the owner would actually look --------------");
  {
    /* It first shipped at the foot of the Stores panel, below the store rows
     * and the search results, and the owner — who had asked for it — could not
     * find it: "I don't see it." A control for "the app is showing me the wrong
     * thing" has to be where you would look while thinking that, so it has its
     * own panel behind the gear in the top bar. */
    const b = boot();
    await drain();
    check("there is a settings button in the top bar", !!b.gear);
    check("it sits in the header, beside the other top-bar buttons",
      b.gear && b.gear.parentElement.tagName === "HEADER", b.gear && b.gear.parentElement.tagName);
    check("the panel starts closed", b.panel.style.display !== "flex", b.panel.style.display);
    b.gear.click();
    await drain();
    check("the gear opens it", b.panel.style.display === "flex", b.panel.style.display);
    check("the reload control is inside that panel", b.panel.contains(b.btn));
    check("so is the build line it refers to", b.panel.contains(b.line));
    check("and it is no longer buried in the Stores panel",
      !b.w.document.getElementById("stores-panel").contains(b.btn));
    b.w.document.getElementById("set-close").click();
    check("✕ closes it", b.panel.style.display === "none", b.panel.style.display);
  }

  console.log("\n--- 1. the control is on the page, in both languages ------------");
  {
    const b = boot({ lang: "en" });
    await drain();
    check("button is present", !!b.btn);
    check("the panel is titled in English", /Settings/.test(b.w.document.getElementById("set-h1").textContent),
      b.w.document.getElementById("set-h1").textContent);
    check("labelled in English", /Reload the app/.test(b.btn.textContent), b.btn.textContent);
    check("carries the ↻ the stale banner points at", /↻/.test(b.btn.textContent), b.btn.textContent);
  }
  {
    const b = boot({ lang: "ja" });
    await drain();
    check("labelled in Japanese", /再読み込み/.test(b.btn.textContent), b.btn.textContent);
    check("the panel is titled in Japanese", /設定/.test(b.w.document.getElementById("set-h1").textContent),
      b.w.document.getElementById("set-h1").textContent);
    check("Japanese label carries ↻ too", /↻/.test(b.btn.textContent), b.btn.textContent);
  }

  console.log("\n--- 2. the stale banner no longer names gestures that do nothing --");
  {
    /* It used to say "Pull down / reopen to update". A WebView has no
     * pull-to-refresh, so a phone could follow that advice exactly and stay
     * stale. Advice that cannot work is worse than none — it spends the one
     * moment the owner was willing to act. */
    /* Read off the rendered element rather than the dictionary: /health here
     * reports a build the page is not on, which is exactly the state a stale
     * handset is in, so this is the sentence the owner would actually see. */
    const b = boot();
    await drain(); await drain();
    const en = b.line.textContent;
    check("the banner fired at all", b.line.classList.contains("stale"), en);
    check("does not tell anyone to pull down", !/pull down/i.test(en), en);
    check("points at the button instead", /↻/.test(en), en);

    const j = boot({ lang: "ja" });
    await drain(); await drain();
    const ja = j.line.textContent;
    check("Japanese banner fired", j.line.classList.contains("stale"), ja);
    check("Japanese points at the button too", /↻/.test(ja), ja);
    check("Japanese no longer just says 再読み込みしてください",
      !/再読み込みしてください。$/.test(ja), ja);
  }

  console.log("\n--- 3. the fresh copy is fetched, past the cache -----------------");
  {
    const b = boot();
    await drain();
    b.btn.click();
    await drain(); await drain();
    const page = b.fetches.filter(f => f.url === "/");
    check("asked the server for the page", page.length === 1, b.fetches.map(f => f.url));
    check("asked past the cache, not politely",
      page[0] && page[0].opts.cache === "reload", page[0] && page[0].opts);
  }

  console.log("\n--- 4. the worker is replaced, and nothing is torn down ----------");
  {
    /* An earlier draft cleared Cache Storage and unregistered the workers here.
     * It fetched the page first, so it could not strand a phone whose tailnet
     * was already down — but connectivity dropping between that fetch and the
     * reload would leave no offline shell AND no worker to serve one, on a page
     * whose no-cache headers make the reload ask the network again.
     *
     * update() has no such window: the new worker fills its cache in install
     * and evicts the old ones in activate, so eviction is atomic with having
     * somewhere to evict to, and never happens at all if the install fails. */
    const b = boot({ pageOk: true });
    await drain();
    b.btn.click();
    await drain(); await drain(); await drain();
    check("the worker was told to fetch its replacement", b.updated.length === 1, b.updated);
    check("no cache was deleted from the page", b.deleted.length === 0, b.deleted);
    check("no worker was unregistered", b.unregistered.length === 0, b.unregistered);
    check("and only then did the page reload", b.reloads.length === 1, b.reloads);
  }

  console.log("\n--- 4b. an update that fails still leaves a working app ----------");
  {
    /* Staying on the old worker is an old app. Having no worker is no app. */
    const b = boot({ pageOk: true });
    await drain();
    Object.defineProperty(b.w.navigator, "serviceWorker", {
      configurable: true,
      value: { register: () => {}, getRegistrations: async () => { throw new Error("no"); } },
    });
    b.btn.click();
    await drain(); await drain(); await drain();
    check("the reload still happened", b.reloads.length === 1, b.reloads);
    check("and nothing was torn down on the way",
      b.deleted.length === 0 && b.unregistered.length === 0, [b.deleted, b.unregistered]);
  }

  console.log("\n--- 5. unreachable server: nothing is dropped, nothing is lost ----");
  {
    /* The failure that would matter. Clearing the shell and THEN finding the
     * tailnet unreachable leaves the phone with no app, in a shop, which is
     * where it is used. */
    const b = boot({ pageOk: false });
    await drain();
    b.btn.click();
    await drain(); await drain(); await drain();
    check("no cache was deleted", b.deleted.length === 0, b.deleted);
    check("the worker still serves the shell", b.unregistered.length === 0, b.unregistered);
    check("the worker was not even asked to update", b.updated.length === 0, b.updated);
    check("the page was not reloaded into nothing", b.reloads.length === 0, b.reloads);
    check("the failure is stated, not swallowed", b.err.classList.contains("on"), b.err.textContent);
    check("and it says the copy in hand still works",
      /working copy/.test(b.err.textContent), b.err.textContent);
    check("the button comes back for a second try", b.btn.disabled === false);
    check("relabelled, not left saying 'fetching'",
      /Reload the app/.test(b.btn.textContent), b.btn.textContent);
  }

  console.log("\n--- 6. a double tap cannot run it twice --------------------------");
  {
    const b = boot();
    await drain();
    b.btn.click();
    b.btn.click();
    await drain(); await drain(); await drain();
    check("one fetch of the page, not two",
      b.fetches.filter(f => f.url === "/").length === 1, b.fetches.map(f => f.url));
    check("one reload", b.reloads.length === 1, b.reloads);
  }

  console.log(`\n================ ${passed} passed, ${failed} failed ================`);
  process.exit(failed === 0 ? 0 : 1);
})();
