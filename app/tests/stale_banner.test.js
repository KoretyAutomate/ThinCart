/**
 * stale_banner.test.js — an old page says so where the list is.
 *
 * WHY THIS FILE EXISTS. Five rounds of "long press doesn't work" were reported
 * from a phone running a page that predated the fix being tested. Each trace
 * was read as evidence about the current code; none of them was. The app
 * already detected staleness — `showBuild()` compared the page's own stamp
 * with `/health` and turned the build line red — but that line lives in ⚙️,
 * and nobody opens ⚙️ while shopping.
 *
 * So the same fact is now stated on the list, with a button that fixes it, and
 * every gesture trace carries the build that produced it. A report can no
 * longer be ambiguous about which version it came from.
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
const settle = () => new Promise(r => setTimeout(r, 20));

/** `serverBuild` is what /health reports. The page's own stamp is the
 *  placeholder the server substitutes at serve time, so anything else is a
 *  page that has fallen behind. */
function boot(serverBuild, { pageFails = false } = {}) {
  const dom = new JSDOM(HTML, { runScripts: "outside-only", url: "https://s.ts.net/" });
  const w = dom.window;
  w.localStorage.setItem("pc_name", "tester");
  w.localStorage.setItem("pc_base", JSON.stringify(
    { revision: 1, items: [], stores: [], picks: {}, suggestions: [], away_pending: 0 }));
  const fetched = [];
  w.fetch = (url, opts) => {
    fetched.push(String(url));
    if (String(url).includes("/health"))
      return Promise.resolve({ ok: true, status: 200, json: async () => ({ ok: true, build: serverBuild }) });
    if (String(url) === "/")
      return pageFails ? Promise.reject(new TypeError("Failed to fetch"))
                       : Promise.resolve({ ok: true, status: 200, text: async () => "<html>fresh</html>" });
    return Promise.resolve({ ok: true, status: 200, json: async () => ({}), text: async () => "" });
  };
  w.WebSocket = function () { this.close = () => {}; };
  w.eval(SCRIPT);
  const reloads = [];
  w.hardReload = () => reloads.push(1);
  return { w, doc: w.document, fetched, reloads,
           bar: () => w.document.getElementById("stalebar"),
           text: () => w.document.getElementById("stalebar").textContent };
}

(async () => {
  console.log("\n--- 1. a page that has fallen behind says so, on the list ----------");
  {
    const b = boot("deadbeef");                 // the server is serving something else
    await settle(); await settle();
    check("the banner is up", b.bar().classList.contains("on"), b.text());
    check("in plain words", /old version/.test(b.text()), b.text());
    check("with something to press", /Update now/.test(b.text()), b.text());
  }

  console.log("\n--- 2. and pressing it reloads --------------------------------------");
  {
    const b = boot("deadbeef");
    await settle(); await settle();
    b.doc.getElementById("stalebar-go").click();
    await settle(); await settle();
    check("the page was fetched past the cache",
      b.fetched.includes("/"), b.fetched);
    check("and the app reloaded", b.reloads.length === 1, b.reloads);
  }

  console.log("\n--- 2b. a reload that fails says so ON THE BAR -----------------------");
  {
    /* The bar lives on the list and the settings panel is shut. Writing the
     * progress and the failure into the panel's elements would make a failed
     * update indistinguishable from nothing happening. Reviewer, 2026-09-12. */
    const b = boot("deadbeef", { pageFails: true });
    await settle(); await settle();
    b.doc.getElementById("stalebar-go").click();
    await settle(); await settle();
    check("the failure is on the bar, where the button is",
      /Could not reach the server/.test(b.text()), b.text());
    check("nothing was reloaded", b.reloads.length === 0, b.reloads);
    check("and the button is pressable again",
      !b.doc.getElementById("stalebar-go").disabled);
    check("still offering to update", /Update now/.test(b.text()), b.text());
  }

  console.log("\n--- 3. a current page says nothing ----------------------------------");
  {
    // __BUILD__ is the placeholder the server replaces as it serves; a page
    // whose stamp matches what /health reports is current by definition.
    const b = boot("__BUILD__");
    await settle(); await settle();
    check("no banner", !b.bar().classList.contains("on"), b.text());
  }
  {
    const b = boot(null);                        // /health said nothing useful
    await settle(); await settle();
    check("an unanswerable check does not cry wolf", !b.bar().classList.contains("on"), b.text());
  }

  console.log("\n--- 4. every trace carries the build that produced it ---------------");
  {
    /* Five traces were read as evidence about code the phone was not running.
     * A trace that names its build cannot be misread that way again. */
    const b = boot("__BUILD__");
    await settle();
    b.doc.getElementById("set-btn").click();
    const diag = b.doc.getElementById("diag-out").textContent;
    check("the trace line names the build", /^build \S+/.test(diag), diag);
  }

  console.log(`\n================ ${passed} passed, ${failed} failed ================`);
  process.exit(failed === 0 ? 0 : 1);
})();
