/**
 * calendar_step.test.js — the launcher sends the calendar, and never blocks on it.
 *
 * PLAN.md §2026-09-26: travel detection's calendar is read on the Pixel, in
 * this launcher, because it is the page the native bridge is injected for.
 * What must hold:
 *   - granted → push to the saved server, then open the app;
 *   - first run asks once; after a refusal it never asks again;
 *   - no plugin, a refusal, a failing push, or a dialog that never settles all
 *     still open the app — the calendar is never a gate.
 *
 * Run: npm test
 */
const fs = require("fs");
const path = require("path");
const { JSDOM } = require("jsdom");

const html = fs.readFileSync(path.join(__dirname, "..", "www", "index.html"), "utf8");
let passed = 0, failed = 0;
const check = (name, cond, detail = "") => {
  if (cond) { passed++; console.log(`[PASS] ${name}`); }
  else { failed++; console.log(`[FAIL] ${name}  ${JSON.stringify(detail)}`); }
};
const wait = ms => new Promise(r => setTimeout(r, ms));
const SERVER = "https://spark.example.ts.net";

/** `state` is what checkPermissions reports, `answer` what the dialog returns. */
function boot({ state = "granted", answer = "granted", pushFails = false, hang = false } = {}) {
  const log = [];
  const dom = new JSDOM(html, { runScripts: "outside-only", url: "https://localhost/", pretendToBeVisual: true });
  const w = dom.window;
  w.localStorage.setItem("thincart.server", SERVER);
  w.Capacitor = { Plugins: { Calendar: {
    checkPermissions: () => { log.push("check"); return hang ? new Promise(() => {}) : Promise.resolve({ calendar: state }); },
    requestPermissions: (o) => { log.push("request:" + o.permissions.join()); return Promise.resolve({ calendar: answer }); },
    push: (o) => { log.push("push:" + o.url); return pushFails ? Promise.reject(new Error("boom")) : Promise.resolve({ queued: true }); },
  } } };
  w.fetch = (url) => {
    if (String(url).endsWith("/version")) log.push("version");
    return String(url).endsWith("/version")
      ? Promise.resolve({ ok: false, status: 404, json: async () => ({}) })
      : Promise.resolve({ type: "opaque" });
  };
  const script = html.split("<script>")[1].split("</script>")[0];
  w.eval(script);
  w.openServer = (url) => log.push("open:" + url);
  return { w, log };
}

(async () => {
  console.log("\n--- 1. granted: push to the saved server, then open ---------------");
  {
    const b = boot();
    await wait(20);
    check("pushed to the saved server", b.log.includes("push:" + SERVER), b.log);
    check("did not ask", !b.log.some(e => e.startsWith("request")), b.log);
    check("pushed before the handoff",
      b.log.indexOf("push:" + SERVER) >= 0 && b.log.indexOf("push:" + SERVER) < b.log.indexOf("open:" + SERVER), b.log);
  }

  console.log("\n--- 2. first run: ask once, then push ----------------------------");
  {
    const b = boot({ state: "prompt", answer: "granted" });
    await wait(20);
    check("asked for the calendar permission", b.log.includes("request:calendar"), b.log);
    check("then pushed", b.log.includes("push:" + SERVER), b.log);
    check("then opened", b.log.includes("open:" + SERVER), b.log);
  }

  console.log("\n--- 3. refused in the dialog: no push, app opens -----------------");
  {
    const b = boot({ state: "prompt", answer: "denied" });
    await wait(20);
    check("no push", !b.log.some(e => e.startsWith("push")), b.log);
    check("app opens", b.log.includes("open:" + SERVER), b.log);
  }

  console.log("\n--- 4. refused before: never asked again -------------------------");
  for (const state of ["prompt-with-rationale", "denied"]) {
    const b = boot({ state });
    await wait(20);
    check(`${state}: no dialog`, !b.log.some(e => e.startsWith("request")), b.log);
    check(`${state}: no push`, !b.log.some(e => e.startsWith("push")), b.log);
    check(`${state}: app opens`, b.log.includes("open:" + SERVER), b.log);
  }

  console.log("\n--- 5. a failing push is not the app's problem -------------------");
  {
    const b = boot({ pushFails: true });
    await wait(20);
    check("app opens anyway", b.log.includes("open:" + SERVER), b.log);
  }

  console.log("\n--- 6. a permission call that never settles cannot hang the launcher");
  {
    const b = boot({ hang: true });
    const t0 = Date.now();
    const outcome = await b.w.syncCalendar(SERVER, 60);
    check("the step gives up at its cap", outcome === "timeout", outcome);
    check("and does so promptly", Date.now() - t0 < 1000, Date.now() - t0);
    // the launch flow calls the same seam, so a capped step is a launched app
    const c = boot({ hang: true });
    c.w.syncCalendar = () => Promise.resolve("timeout");
    await wait(20);
    check("a timed-out step still opens the app", c.log.includes("open:" + SERVER), c.log);
  }

  console.log(`\n================ ${passed} passed, ${failed} failed ================`);
  process.exit(failed === 0 ? 0 : 1);
})();
