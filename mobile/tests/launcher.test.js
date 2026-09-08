/**
 * launcher.test.js — the APK shell's one piece of logic.
 *
 * Loads the REAL mobile/www/index.html in jsdom, same discipline as the
 * outfit-advisor suites.
 *
 * Two things are under test, and both fail in ways that look like "the app is
 * broken" rather than "the launcher is wrong":
 *   - normalizeServerUrl() decides http-vs-https from what the owner typed. Get
 *     it backwards and the Tailscale name is fetched over plain http (the serve
 *     endpoint isn't there) or the tailnet IP over https (no cert) — either way
 *     an unreachable server with nothing to say about why.
 *   - the cold-start / returned-with-Back branch. If a Back press re-triggers
 *     the auto-launch, the settings screen is unreachable and a mistyped-but-
 *     reachable address can never be corrected without clearing app data.
 *
 * Run: npm test   (jsdom is a devDependency)
 */
const fs = require("fs");
const path = require("path");
const { JSDOM } = require("jsdom");

const HTML = path.join(__dirname, "..", "www", "index.html");
let passed = 0, failed = 0;
const check = (name, cond, detail = "") => {
  if (cond) { passed++; console.log(`[PASS] ${name}`); }
  else { failed++; console.log(`[FAIL] ${name}  ${JSON.stringify(detail)}`); }
};

const html = fs.readFileSync(HTML, "utf8");
const drain = () => new Promise(r => setTimeout(r, 0));

/* Boot a fresh launcher. `saved` seeds localStorage (a previous install),
 * `launched` seeds sessionStorage (we came back with the Back button), and
 * `reachable` decides what the probe's fetch does. */
function boot({ saved = null, launched = false, reachable = true, record = null,
                capacitor = null, version = null } = {}) {
  const navigated = [];
  const dom = new JSDOM(html, {
    runScripts: "outside-only", url: "https://localhost/", pretendToBeVisual: true,
  });
  const w = dom.window;

  if (saved) w.localStorage.setItem("thincart.server", saved);
  if (launched) w.sessionStorage.setItem("thincart.launched", "1");

  // The native bridge exists only inside the APK; a test that hands one in is
  // the APK, a test that does not is a browser.
  if (capacitor) w.Capacitor = { Plugins: { AppUpdate: capacitor } };
  w.fetch = (url, opts) => {
    if (record) record.push({ url, opts });
    if (!reachable) return Promise.reject(new TypeError("Failed to fetch"));
    if (String(url).endsWith("/version"))
      return Promise.resolve(version ? { ok: true, status: 200, json: async () => version }
                                     : { ok: false, status: 404, json: async () => ({}) });
    return Promise.resolve({ type: "opaque" });
  };

  const script = html.split("<script>")[1].split("</script>")[0];
  w.eval(script);

  // jsdom refuses real navigation and locks location.assign, so the launcher's
  // openServer() seam is what gets stubbed. Overridden after eval, before any
  // probe resolves — start() only reaches openServer via an awaited promise.
  w.openServer = (url) => navigated.push(url);
  return { w, navigated, visible: () => ["connecting", "setup", "settings", "update"]
    .find(s => w.document.getElementById("screen-" + s).classList.contains("on")) };
}

console.log("\n--- 1. normalizeServerUrl: scheme is inferred from the host shape ---");
const { w: nw } = boot();
const norm = nw.normalizeServerUrl;
const CASES = [
  // [input, expected]
  ["spark-d28c.example-tailnet.ts.net", "https://spark-d28c.example-tailnet.ts.net"],
  ["  spark-d28c.example-tailnet.ts.net/ ", "https://spark-d28c.example-tailnet.ts.net"],
  ["https://spark-d28c.example-tailnet.ts.net/", "https://spark-d28c.example-tailnet.ts.net"],
  ["100.112.171.54:8123", "http://100.112.171.54:8123"],          // bare IP => http
  ["100.112.171.54", "http://100.112.171.54"],
  ["https://100.112.171.54:8123", "https://100.112.171.54:8123"], // explicit scheme wins
  ["http://spark.example.ts.net", "http://spark.example.ts.net"],
  ["localhost:8123", "https://localhost:8123"],
];
for (const [input, expected] of CASES) {
  check(`normalize ${JSON.stringify(input)}`, norm(input) === expected, { got: norm(input), expected });
}

console.log("\n--- 2. normalizeServerUrl: junk is rejected, not guessed at ---");
const BAD = ["", "   ", "not a url", "ftp://spark.ts.net", "javascript:alert(1)", "spark", null, undefined, 42];
for (const bad of BAD) {
  check(`reject ${JSON.stringify(bad)}`, norm(bad) === null, { got: norm(bad) });
}

(async () => {
  console.log("\n--- 3. cold start with a saved, reachable server: straight in ------");
  {
    const b = boot({ saved: "https://spark.example.ts.net", reachable: true });
    check("shows the connecting screen first", b.visible() === "connecting", b.visible());
    await drain(); await drain();
    check("navigates to the saved server", b.navigated[0] === "https://spark.example.ts.net", b.navigated);
    check("marks the session as launched",
      b.w.sessionStorage.getItem("thincart.launched") === "1");
  }

  console.log("\n--- 4. cold start, server unreachable: says so, stays put ---------");
  {
    const b = boot({ saved: "https://spark.example.ts.net", reachable: false });
    await drain(); await drain();
    check("does not navigate", b.navigated.length === 0, b.navigated);
    check("lands on settings", b.visible() === "settings", b.visible());
    check("explains why", /Tailscale/.test(b.w.document.getElementById("settings-err").textContent),
      b.w.document.getElementById("settings-err").textContent);
    check("keeps the saved address for a retry",
      b.w.localStorage.getItem("thincart.server") === "https://spark.example.ts.net");
  }

  console.log("\n--- 5. returned with Back: settings, NOT another auto-launch ------");
  {
    const b = boot({ saved: "https://spark.example.ts.net", launched: true, reachable: true });
    await drain(); await drain();
    check("does not bounce straight back out", b.navigated.length === 0, b.navigated);
    check("shows settings", b.visible() === "settings", b.visible());
    check("names the current server",
      b.w.document.getElementById("current-url").textContent === "https://spark.example.ts.net");
  }

  console.log("\n--- 6. first run: setup, and Connect remembers the address --------");
  {
    const b = boot({ saved: null, reachable: true });
    check("shows setup", b.visible() === "setup", b.visible());
    b.w.document.getElementById("url").value = "spark.example.ts.net";
    b.w.document.getElementById("connect").click();
    await drain(); await drain();
    check("saves the normalized address",
      b.w.localStorage.getItem("thincart.server") === "https://spark.example.ts.net",
      b.w.localStorage.getItem("thincart.server"));
    check("opens it", b.navigated[0] === "https://spark.example.ts.net", b.navigated);
  }

  console.log("\n--- 7. first run, junk typed: refuses without saving anything -----");
  {
    const b = boot({ saved: null, reachable: true });
    b.w.document.getElementById("url").value = "not a url";
    b.w.document.getElementById("connect").click();
    await drain(); await drain();
    check("nothing saved", b.w.localStorage.getItem("thincart.server") === null);
    check("nothing opened", b.navigated.length === 0, b.navigated);
    check("stays on setup", b.visible() === "setup", b.visible());
    check("says what a good address looks like",
      /ts\.net/.test(b.w.document.getElementById("setup-err").textContent),
      b.w.document.getElementById("setup-err").textContent);
  }

  console.log("\n--- 8. 'use a different server' returns to setup, prefilled -------");
  {
    const b = boot({ saved: "https://old.example.ts.net", launched: true, reachable: true });
    await drain();
    b.w.document.getElementById("change").click();
    check("setup screen", b.visible() === "setup", b.visible());
    check("prefilled with the old address",
      b.w.document.getElementById("url").value === "https://old.example.ts.net");
  }

  console.log("\n--- 9. handoff that never completes: says so, offers a way out ---");
  {
    // What v1.0 did on the phone: probe fine, navigation refused by
    // allowNavigation, launcher left on screen with its spinner and no message.
    // The message deliberately names no cause — a refusal (Capacitor fires
    // ACTION_VIEW, so the page opens in the browser) and a merely-slow commit
    // cannot be told apart from in here, and backgrounding does not separate
    // them either since locking the phone backgrounds the app too.
    const b = boot({ saved: "https://spark.example.ts.net", reachable: true });
    b.w.openServer = () => { /* never completes, for whichever reason */ };
    // The launcher runs under "use strict", so its top-level vars are not window
    // properties and the grace period cannot be shortened from out here. Capture
    // the timer instead: launch() schedules it via window.setTimeout, and the
    // probe's own timer was already created (and cleared) during eval.
    let handoffCb = null;
    b.w.setTimeout = (fn) => { handoffCb = fn; return 0; };
    await drain(); await drain(); await drain();
    check("was on the connecting screen at handoff", b.visible() === "connecting", b.visible());
    check("scheduled a check", typeof handoffCb === "function");
    if (handoffCb) handoffCb();
    check("falls back to settings", b.visible() === "settings", b.visible());
    const msg = b.w.document.getElementById("settings-err").textContent;
    check("says the server was reachable", /server answered/.test(msg), msg);
    check("offers the address as the thing to check", /check the\s+address/.test(msg), msg);
    check("claims no cause it cannot know",
      !/because|disallowed|refused to/.test(msg), msg);
  }

  console.log("\n--- 10. successful handoff cancels the refusal timer (Codex P2) ---");
  {
    // A successful navigation may park this document in the WebView's
    // back-forward cache, where the timer is paused, not dropped. Back within
    // four seconds would resume it and report a refusal that never happened.
    const b = boot({ saved: "https://spark.example.ts.net", reachable: true });
    // Unique ids per timer rather than one fixed value: the probe schedules its
    // own abort timer and clears it, and a shared stub id made that tidy-up read
    // as the refusal timer being cancelled. The refusal timer is the last issued.
    // Offset well clear of jsdom's own counter: the probe's abort timer was
    // scheduled with the REAL setTimeout before these stubs were installed, and
    // its id (1) collided with a synthetic one, so its ordinary tidy-up looked
    // like the refusal timer being cancelled.
    const issued = [], cleared = [];
    b.w.setTimeout = () => { issued.push(9000 + issued.length); return issued[issued.length - 1]; };
    b.w.clearTimeout = (id) => cleared.push(id);
    await drain(); await drain(); await drain();
    check("navigated", b.navigated[0] === "https://spark.example.ts.net", b.navigated);
    const TIMER_ID = issued[issued.length - 1];
    check("refusal timer not cancelled while still on the launcher",
      !cleared.includes(TIMER_ID), { TIMER_ID, issued, cleared });
    b.w.dispatchEvent(new b.w.Event("pagehide"));
    check("pagehide cancels the refusal timer", cleared.includes(TIMER_ID), cleared);
    check("no false refusal shown",
      b.w.document.getElementById("settings-err").textContent === "",
      b.w.document.getElementById("settings-err").textContent);
  }

  console.log("\n--- 11. Force a fresh copy: a cache-buster, and no double fetch ---");
  {
    /* The hammer for a page so old it predates the reload button inside the app.
     * Before it existed the only way out was Android's app-storage screen. */
    const calls = [];
    const b = boot({ saved: "https://spark.example.ts.net", launched: true, reachable: true, record: calls });
    await drain();
    check("Back landed on the settings screen", b.visible() === "settings", b.visible());
    b.w.document.getElementById("fresh").click();
    await drain(); await drain(); await drain();
    const target = b.navigated[0] || "";
    check("navigated with a cache-buster", /^https:\/\/spark\.example\.ts\.net\?fresh=\d+$/.test(target), target);
    // Only /health goes out from here. The page is fetched by the navigation
    // itself, on the server's own origin — which is where the service worker
    // lives, and the only cache partition that navigation ever reads.
    check("the launcher fetches nothing but the probe",
      calls.every(c => c.url.endsWith("/health")), calls.map(c => c.url));
  }

  console.log("\n--- 12. launchUrl: the buster attaches without corrupting the address ---");
  {
    const { w } = boot();
    const plain = w.launchUrl("https://spark.example.ts.net", false);
    check("not forced => untouched", plain === "https://spark.example.ts.net", plain);
    const forced = w.launchUrl("https://spark.example.ts.net", true);
    check("forced => ?fresh", /^https:\/\/spark\.example\.ts\.net\?fresh=\d+$/.test(forced), forced);
    const withQuery = w.launchUrl("https://spark.example.ts.net/?a=1", true);
    check("an existing query keeps its ? and gets &",
      /^https:\/\/spark\.example\.ts\.net\/\?a=1&fresh=\d+$/.test(withQuery), withQuery);
  }

  /* ---- in-app updates: the reason this launcher can reach the installer ---- */
  const INSTALLED = { versionCode: 2, versionName: "1.1", canInstall: true };
  const NEWER = { versionCode: 3, versionName: "1.2", file: "thincart.apk", size: 3409665, sha256: "abc123" };
  const plugin = (installs) => ({
    current: async () => INSTALLED,
    install: async (o) => { installs.push(o); return { status: "installer-opened", bytes: 1 }; },
  });

  console.log("\n--- 13. a newer build on the DGX is offered before the handoff ------");
  {
    const installs = [];
    const b = boot({ saved: "https://spark.example.ts.net", reachable: true, capacitor: plugin(installs), version: NEWER });
    await drain(); await drain(); await drain(); await drain();
    check("the update screen is shown", b.visible() === "update", b.visible());
    check("and the app was NOT opened underneath it", b.navigated.length === 0, b.navigated);
    const sub = b.w.document.getElementById("update-sub").textContent;
    check("it says which build, from which, and that nothing is lost",
      /v1\.1/.test(sub) && /3\.4 MB/.test(sub) && /kept/.test(sub), sub);
    b.w.document.getElementById("update-install").click();
    await drain(); await drain();
    check("Install hands the DGX's /apk and its checksum to the native installer",
      installs.length === 1 && installs[0].url === "https://spark.example.ts.net/apk"
        && installs[0].sha256 === "abc123" && installs[0].size === 3409665, installs);
    const msg = b.w.document.getElementById("update-err").textContent;
    check("and says what happens next", /installer is open/i.test(msg), msg);
  }

  console.log("\n--- 14. Later opens the app, and does not ask again this run --------");
  {
    const b = boot({ saved: "https://spark.example.ts.net", reachable: true, capacitor: plugin([]), version: NEWER });
    await drain(); await drain(); await drain(); await drain();
    b.w.document.getElementById("update-later").click();
    await drain();
    check("the app opens", b.navigated.length === 1, b.navigated);
    b.w.document.getElementById("open").click();     // the same run, again
    await drain(); await drain(); await drain(); await drain();
    check("no second offer for the same build", b.visible() !== "update" && b.navigated.length === 2,
      { visible: b.visible(), navigated: b.navigated });
  }

  console.log("\n--- 15. nothing to offer means the app opens as usual ---------------");
  {
    const b = boot({ saved: "https://spark.example.ts.net", reachable: true, capacitor: plugin([]),
                     version: { ...NEWER, versionCode: 2 } });          // same build
    await drain(); await drain(); await drain(); await drain();
    check("same version: straight in", b.navigated.length === 1, b.navigated);
  }
  {
    const b = boot({ saved: "https://spark.example.ts.net", reachable: true, capacitor: plugin([]), version: null });
    await drain(); await drain(); await drain(); await drain();
    check("nothing published (404): straight in", b.navigated.length === 1, b.navigated);
  }
  {
    const calls = [];
    const b = boot({ saved: "https://spark.example.ts.net", reachable: true, version: NEWER, record: calls });
    await drain(); await drain(); await drain(); await drain();
    check("a browser (no native bridge): straight in", b.navigated.length === 1, b.navigated);
    check("and /version is not even asked for", !calls.some(c => String(c.url).endsWith("/version")), calls.map(c => c.url));
  }

  console.log("\n--- 16. the settings screen says which build this is ----------------");
  {
    const b = boot({ saved: "https://spark.example.ts.net", launched: true, capacitor: plugin([]), version: NEWER });
    await drain(); await drain(); await drain(); await drain();
    const line = b.w.document.getElementById("version-line").textContent;
    check("names the installed build and the waiting one", /v1\.1/.test(line) && /v1\.2 available/.test(line), line);
  }

  console.log(`\n================ ${passed} passed, ${failed} failed ================`);
  process.exit(failed === 0 ? 0 : 1);
})();
