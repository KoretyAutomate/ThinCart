/**
 * page_scope.test.js — the page run the way a browser runs it.
 *
 * WHY THIS FILE EXISTS. Every other suite here loads index.html and then
 * executes its script with `w.eval(SCRIPT)`. That is convenient — the mocks
 * can be installed first — and it is a different language feature: a
 * strict-mode `eval` gives the script its OWN scope, so top-level `function f`
 * is a local binding rather than `window.f`.
 *
 * That difference hid a fatal bug for six rounds. index.html carried
 *
 *     window.openSheet = it => openSheet(it);
 *
 * which under `eval` is a harmless alias of a local function, and in a real
 * page replaces the global `openSheet` with a wrapper that calls itself —
 * infinite recursion, RangeError, and no editor, ever. Every gesture fix was
 * correct; none of them could have worked; and 200+ green checks said nothing
 * about it, because they were exercising a function the browser never calls.
 *
 * So this file boots the page with `runScripts: "dangerously"`, the mocks
 * installed in `beforeParse`, and drives the two ways into the editor. It is
 * deliberately small: its job is not to re-test behaviour, but to prove the
 * page's top-level scope is sound in the environment that matters.
 *
 * Run: cd app && npm install && npm test
 */
const fs = require("fs");
const path = require("path");
const { JSDOM } = require("jsdom");

const HTML = fs.readFileSync(path.join(__dirname, "..", "index.html"), "utf8");

let passed = 0, failed = 0;
const check = (name, cond, detail = "") => {
  if (cond) { passed++; console.log(`[PASS] ${name}`); }
  else { failed++; console.log(`[FAIL] ${name}  ${JSON.stringify(detail)}`); }
};
const settle = () => new Promise(r => setTimeout(r, 40));

const ITEM = { id: "i1", catalog_id: 1, name: "milk", name_en: null, category: "dairy", emoji: "",
               note: "", budget: null, qty_note: "", added_by: "t",
               added_at: "2026-09-01T00:00:00+00:00", store: null, store_source: null };

/** The page, parsed and executed as a document — not eval'd into a closure. */
function boot() {
  const errors = [];
  const dom = new JSDOM(HTML, {
    runScripts: "dangerously",
    url: "https://s.ts.net/",
    beforeParse(w) {
      w.localStorage.setItem("pc_name", "tester");
      w.localStorage.setItem("pc_base", JSON.stringify(
        { revision: 1, items: [ITEM], stores: [], picks: {}, suggestions: [], away_pending: 0 }));
      w.fetch = () => Promise.resolve({ ok: true, status: 200, json: async () => ({}), text: async () => "" });
      w.WebSocket = function () { this.close = () => {}; };
      w.addEventListener("error", e => errors.push((e.error && e.error.message) || e.message));
    },
  });
  const w = dom.window, doc = w.document;
  const fire = (el, type, x = 0, y = 0) => {
    const e = new w.Event(type, { bubbles: true, cancelable: true });
    Object.assign(e, { clientX: x, clientY: y, pointerId: 1 });
    el.dispatchEvent(e);
  };
  return { w, doc, errors, fire,
           row: () => doc.querySelector("#list li.item"),
           sheetOpen: () => doc.getElementById("sheet").style.display === "flex",
           trace: () => { doc.getElementById("set-btn").click();
                          return doc.getElementById("diag-out").textContent; } };
}

(async () => {
  console.log("\n--- 1. the page loads without blowing up ----------------------------");
  {
    const b = boot();
    await settle();
    check("the list rendered", !!b.row(), b.doc.getElementById("list").innerHTML.slice(0, 80));
    check("nothing threw on load", b.errors.length === 0, b.errors);
  }

  console.log("\n--- 2. openSheet is a function, not a wrapper around itself ---------");
  {
    /* The exact fault: an arrow assigned over the global function whose body
     * then resolves to the arrow. Calling it recurses until the stack ends. */
    const b = boot();
    await settle();
    check("window.openSheet exists", typeof b.w.openSheet === "function", typeof b.w.openSheet);
    let threw = null;
    try { b.w.openSheet(ITEM); } catch (e) { threw = e.message; }
    check("calling it does not exhaust the stack", threw === null, threw);
    check("it opens the editor", b.sheetOpen());
  }

  console.log("\n--- 3. both ways in work in real page scope --------------------------");
  {
    const b = boot();
    await settle();
    b.fire(b.row().querySelector(".edit"), "click", 350, 200);
    await settle();
    check("the ✎ button opens the editor", b.sheetOpen());
    check("and no error was raised", b.errors.length === 0, b.errors);
    check("the trace confirms it is on screen", /shown:flex/.test(b.trace()), b.trace());
  }
  {
    const b = boot();
    await settle();
    const li = b.row();
    b.fire(li, "pointerdown", 50, 50);
    b.fire(li, "contextmenu", 50, 50);
    await settle();
    check("a long press opens the editor", b.sheetOpen());
    check("and no error was raised", b.errors.length === 0, b.errors);
    check("the trace records the whole path",
      /OPEN:ctx/.test(b.trace()) && /shown:flex/.test(b.trace()), b.trace());
  }

  console.log(`\n================ ${passed} passed, ${failed} failed ================`);
  process.exit(failed === 0 ? 0 : 1);
})();
