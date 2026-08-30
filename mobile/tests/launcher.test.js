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
function boot({ saved = null, launched = false, reachable = true } = {}) {
  const navigated = [];
  const dom = new JSDOM(html, {
    runScripts: "outside-only", url: "https://localhost/", pretendToBeVisual: true,
  });
  const w = dom.window;

  if (saved) w.localStorage.setItem("thincart.server", saved);
  if (launched) w.sessionStorage.setItem("thincart.launched", "1");

  w.fetch = (url) => reachable
    ? Promise.resolve({ type: "opaque" })
    : Promise.reject(new TypeError("Failed to fetch"));

  const script = html.split("<script>")[1].split("</script>")[0];
  w.eval(script);

  // jsdom refuses real navigation and locks location.assign, so the launcher's
  // openServer() seam is what gets stubbed. Overridden after eval, before any
  // probe resolves — start() only reaches openServer via an awaited promise.
  w.openServer = (url) => navigated.push(url);
  return { w, navigated, visible: () => ["connecting", "setup", "settings"]
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
    await drain(); await drain();
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
    const TIMER_ID = 987654;
    const cleared = [];
    b.w.setTimeout = () => TIMER_ID;
    b.w.clearTimeout = (id) => cleared.push(id);
    await drain(); await drain();
    check("navigated", b.navigated[0] === "https://spark.example.ts.net", b.navigated);
    check("refusal timer not cancelled while still on the launcher",
      !cleared.includes(TIMER_ID), cleared);
    b.w.dispatchEvent(new b.w.Event("pagehide"));
    check("pagehide cancels the refusal timer", cleared.includes(TIMER_ID), cleared);
    check("no false refusal shown",
      b.w.document.getElementById("settings-err").textContent === "",
      b.w.document.getElementById("settings-err").textContent);
  }

  console.log(`\n================ ${passed} passed, ${failed} failed ================`);
  process.exit(failed === 0 ? 0 : 1);
})();
