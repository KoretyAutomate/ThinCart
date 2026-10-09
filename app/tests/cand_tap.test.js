/**
 * cand_tap.test.js — tapping a dropdown option must not also open Purchase
 * cycles, and the suggestions tray can be folded (the owner's report,
 * 2026-10-09; the earlier "tapping a suggestion opens purchase history" bug).
 *
 * Cause: the first dropdown row sits directly over the tray label. The row acts
 * on pointerdown and removes the dropdown; the click the same finger still
 * produces then lands on whatever is UNDER it — the label, which opened the
 * cycles panel. jsdom has no hit testing, so the test sends the follow-up click
 * to the label by hand, exactly where the browser would deliver it.
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

const SUGG = (id, name) => ({ catalog_id: id, name, name_en: null, weeks: 1, median_days: 7, tier: "due",
                              events: 4, spread: 1, trusted: true, category: "dairy", emoji: "" });

function boot({ folded = false } = {}) {
  const dom = new JSDOM(HTML, { runScripts: "outside-only", url: "https://s.ts.net/" });
  const w = dom.window;
  const state = { revision: 1, items: [], stores: [], picks: {}, away_pending: 0,
                  suggestions: [SUGG(1, "eggs"), SUGG(2, "bread")] };
  w.localStorage.setItem("pc_name", "tester");
  w.localStorage.setItem("pc_base", JSON.stringify(state));
  const catalog = [
    { name: "milk", name_en: "milk", aliases: [], category: "dairy" },
    { name: "milk chocolate", name_en: null, aliases: [], category: "snacks" }];
  w.localStorage.setItem("pc_catalog", JSON.stringify(catalog));
  if (folded) w.localStorage.setItem("pc_tray_folded", "1");
  const calls = [];
  w.fetch = (url, opts) => {
    calls.push(String(url));
    if (String(url).startsWith("/api/state"))
      return Promise.resolve({ ok: true, status: 200, json: async () => state });
    if (String(url).startsWith("/api/catalog"))
      return Promise.resolve({ ok: true, status: 200, json: async () => ({ catalog }) });
    if (String(url).startsWith("/api/cycles"))
      return Promise.resolve({ ok: true, status: 200, json: async () => ({ cycles: [] }) });
    return Promise.resolve({ ok: true, status: 200, json: async () => ({}), text: async () => "" });
  };
  w.WebSocket = function () { this.close = () => {}; };
  w.eval(SCRIPT);
  const doc = w.document;
  const press = (el) => el.dispatchEvent(new w.Event("pointerdown", { bubbles: true, cancelable: true }));
  const click = (el) => el.dispatchEvent(new w.MouseEvent("click", { bubbles: true, cancelable: true }));
  const type = async (text) => {
    const n = doc.getElementById("name");
    n.value = text; n.dispatchEvent(new w.Event("input", { bubbles: true })); await settle();
  };
  return { w, doc, calls, press, click, type,
           cyc: () => doc.getElementById("cyc").style.display,
           tray: () => doc.getElementById("tray") };
}

(async () => {
  console.log("\n--- 1. a dropdown tap does not open Purchase cycles ---------------");
  {
    const b = boot();
    await settle();
    await b.type("milk");
    const first = b.doc.querySelector("#cands .cand");
    check("the dropdown offers options", !!first);
    b.press(first);                                   // adds, hides the dropdown
    await settle();
    b.click(b.doc.getElementById("tray-label"));      // the finger's click, now over the label
    await settle();
    check("Purchase cycles stayed closed", b.cyc() !== "flex" && !b.calls.includes("/api/cycles"), b.cyc());
    check("and the tap did not fold the tray either", !b.tray().classList.contains("collapsed"));
    b.click(b.doc.getElementById("tray-label"));      // a LATER, deliberate tap is not eaten
    check("a deliberate tap on the label still works (folds)", b.tray().classList.contains("collapsed"));
  }
  {
    // the click may also land on a chip: it must not add that suggestion
    const b = boot();
    await settle();
    await b.type("milk");
    b.press(b.doc.querySelector("#cands .cand"));
    await settle();
    const before = b.w.localStorage.getItem("pc_queue");
    b.click(b.doc.querySelector("#chips .chip"));
    await settle();
    check("a stray click on a chip does not add a suggestion", b.w.localStorage.getItem("pc_queue") === before);
  }
  {
    // a new press cancels the guard: tap elsewhere, then the label, is a real tap
    const b = boot();
    await settle();
    await b.type("milk");
    b.press(b.doc.querySelector("#cands .cand"));
    b.press(b.doc.getElementById("tray-label"));      // the next, separate touch
    b.click(b.doc.getElementById("tray-label"));
    check("a separate next touch is not swallowed", b.tray().classList.contains("collapsed"));
  }

  console.log("\n--- 2. the suggestions tray folds and unfolds --------------------");
  {
    const b = boot();
    await settle();
    const label = b.doc.getElementById("tray-label");
    check("open by default, with its chips",
      !b.tray().classList.contains("collapsed") && b.doc.querySelectorAll("#chips .chip").length === 2
      && label.getAttribute("aria-expanded") === "true");
    b.click(label);
    check("tapping the label folds it",
      b.tray().classList.contains("collapsed") && label.getAttribute("aria-expanded") === "false");
    check("the label no longer opens Purchase cycles", b.cyc() !== "flex");
    check("the fold is remembered", b.w.localStorage.getItem("pc_tray_folded") === "1");
    b.click(label);
    check("tapping again unfolds it", !b.tray().classList.contains("collapsed")
      && b.w.localStorage.getItem("pc_tray_folded") === "0");
  }
  {
    const b = boot({ folded: true });
    await settle();
    check("a remembered fold is applied on load", b.tray().classList.contains("collapsed"));
  }
  {
    const b = boot();
    await settle();
    b.click(b.doc.getElementById("tray-all"));
    await settle();
    check("“All ›” opens the full suggestions panel", b.cyc() === "flex" && b.calls.includes("/api/cycles"), b.cyc());
  }

  console.log(`\n================ ${passed} passed, ${failed} failed ================`);
  if (failed) process.exit(1);
})();
