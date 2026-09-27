/**
 * trip_status.test.js — the Travel panel says where the calendar comes from.
 *
 * Since 2026-09-26 the calendar is read by the Pixel app when it opens (PLAN.md
 * §calendar read on the Pixel). This page cannot read it, so there is no sync
 * button to press — the status line is the whole interface, and each of its
 * three states has to tell the owner what, if anything, to do.
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

function boot(lastSync) {
  const dom = new JSDOM(HTML, { runScripts: "outside-only", url: "https://s.ts.net/" });
  const w = dom.window;
  w.localStorage.setItem("pc_name", "tester");
  w.localStorage.setItem("pc_base", JSON.stringify(
    { revision: 1, items: [], stores: [], picks: {}, suggestions: [], away_pending: 0 }));
  const fetched = [];
  w.fetch = (url, opts) => {
    fetched.push(String(url));
    if (String(url) === "/api/away")
      return Promise.resolve({ ok: true, status: 200, json: async () => (
        { timezone: "America/New_York", last_sync: lastSync, trips: [], rejected: [] }) });
    if (String(url).includes("/health"))
      return Promise.resolve({ ok: true, status: 200, json: async () => ({ ok: true, build: "__BUILD__" }) });
    return Promise.resolve({ ok: true, status: 200, json: async () => ({}), text: async () => "" });
  };
  w.WebSocket = function () { this.close = () => {}; };
  w.eval(SCRIPT);
  return { w, doc: w.document, fetched,
           status: () => w.document.getElementById("trip-status") };
}

async function open(b) {
  b.doc.getElementById("trips").click();
  await settle(); await settle();
}

(async () => {
  console.log("\n--- 1. never read: say how to make it happen ----------------------");
  {
    const b = boot(null);
    await open(b);
    const t = b.status().textContent;
    check("names the Pixel", /Pixel/.test(t), t);
    check("asks for calendar access", /allow calendar access/i.test(t), t);
  }

  console.log("\n--- 2. read: when, and how much -----------------------------------");
  {
    const b = boot({ at: "2026-09-26T15:00:00+00:00", calendars: 1, events: 7, away_days: 3 });
    await open(b);
    const t = b.status().textContent;
    check("says it came from the Pixel", /read from the Pixel/.test(t), t);
    check("with the event count", /7 all-day events/.test(t), t);
  }

  console.log("\n--- 3. read zero calendars: that is a permission problem ----------");
  {
    const b = boot({ at: "2026-09-26T15:00:00+00:00", calendars: 0, events: 0, away_days: 0 });
    await open(b);
    const t = b.status().textContent;
    check("points at Android's permission screen", /Permissions → Calendar/.test(t), t);
  }

  console.log("\n--- 4. nothing on this page tries to sync -------------------------");
  {
    const b = boot(null);
    await open(b);
    check("no sync button", b.doc.getElementById("trip-sync") === null);
    check("no request to the removed endpoint",
      !b.fetched.some(u => u.includes("/api/calendar/sync")), b.fetched);
  }

  console.log(`\n================ ${passed} passed, ${failed} failed ================`);
  process.exit(failed === 0 ? 0 : 1);
})();
