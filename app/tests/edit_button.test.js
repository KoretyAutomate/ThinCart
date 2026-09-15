/**
 * edit_button.test.js — a tap target for the editor, beside the gesture.
 *
 * WHY THIS FILE EXISTS. Long-press to edit has been reported broken six times
 * on the owner's phone. Each round produced a real fix — the event signal, the
 * backdrop dismissing it, the sheet being filled before it was shown, the page
 * being stale — and each time the next trace said it still did not open. Being
 * unable to edit an item is not something to keep waiting on a diagnosis for.
 *
 * So there is now a ✎ button on every row. A button cannot be taken over by
 * gesture arbitration, cancelled by a pan, or swallowed by an overlay: it is
 * the one affordance whose behaviour does not depend on how a WebView decides
 * to interpret a held finger. Long-press stays — it is the nicer gesture, and
 * it works everywhere it is allowed to.
 *
 * What is pinned here: the button opens the editor, and it never also checks
 * the item off, which is what the row's own tap does.
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

function item(id, name) {
  return { id: `i${id}`, catalog_id: id, name, name_en: null, category: "dairy", emoji: "",
           note: "", budget: null, qty_note: "", added_by: "t",
           added_at: "2026-09-01T00:00:00+00:00", store: null, store_source: null };
}

function boot() {
  const dom = new JSDOM(HTML, { runScripts: "outside-only", url: "https://s.ts.net/" });
  const w = dom.window;
  w.localStorage.setItem("pc_name", "tester");
  w.localStorage.setItem("pc_base", JSON.stringify(
    { revision: 1, items: [item(1, "milk"), item(2, "bread")], stores: [], picks: {},
      suggestions: [], away_pending: 0 }));
  w.fetch = () => Promise.resolve({ ok: true, status: 200, json: async () => ({}), text: async () => "" });
  w.WebSocket = function () { this.close = () => {}; };
  w.eval(SCRIPT);
  const doc = w.document;
  const fire = (el, type) => {
    const e = new w.Event(type, { bubbles: true, cancelable: true });
    Object.assign(e, { clientX: 0, clientY: 0, pointerId: 1 });
    el.dispatchEvent(e);
    return e;
  };
  return { w, doc, fire,
           rows: () => [...doc.querySelectorAll("#list li.item")],
           sheetOpen: () => doc.getElementById("sheet").style.display === "flex",
           title: () => doc.getElementById("sheet-title").textContent,
           queued: () => { try { return JSON.parse(w.localStorage.getItem("pc_queue") || "[]"); }
                           catch (e) { return []; } },
           trace: () => { doc.getElementById("set-btn").click();
                          return doc.getElementById("diag-out").textContent; } };
}

(async () => {
  console.log("\n--- 1. every row has one, and it opens that row's editor -----------");
  {
    const b = boot();
    const btns = b.rows().map(li => li.querySelector(".edit"));
    check("one on each row", btns.length === 2 && btns.every(Boolean), btns.length);
    check("it reads as an edit", btns[0].textContent === "✎", btns[0].textContent);
    b.fire(btns[1], "click");
    await settle();
    check("the editor opened", b.sheetOpen());
    check("for the row that was pressed", /bread/.test(b.title()), b.title());
  }

  console.log("\n--- 2. and it is never also a check-off ------------------------------");
  {
    /* The row's own tap buys the item. A button sitting inside that row must
     * not do both — losing an item off the list because you meant to edit it
     * is worse than not being able to edit it. */
    const b = boot();
    const btn = b.rows()[0].querySelector(".edit");
    b.fire(btn, "pointerdown");
    b.fire(btn, "pointerup");
    b.fire(btn, "click");
    await settle();
    check("nothing was bought", !b.queued().some(o => o.type === "checkoff"), b.queued());
    check("the editor is open instead", b.sheetOpen());
  }

  console.log("\n--- 2b. a finger that drifts on the button must not buy the item ----");
  {
    /* Stopping `pointerdown` alone left `pointermove` and `pointerup` bubbling
     * into the row's swipe handler, whose start coordinates were never set —
     * so a press at a real screen position plus one pixel of drift measured as
     * a swipe right across the row and CHECKED THE ITEM OFF. Reviewer,
     * 2026-09-15: replayed at (350, 200) on a 366 px row. */
    const b = boot();
    const btn = b.rows()[0].querySelector(".edit");
    const at = (type, x, y) => {
      const e = new b.w.Event(type, { bubbles: true, cancelable: true });
      Object.assign(e, { clientX: x, clientY: y, pointerId: 1 });
      btn.dispatchEvent(e);
    };
    at("pointerdown", 350, 200);
    at("pointermove", 351, 200);
    at("pointerup", 351, 200);
    at("click", 351, 200);
    await settle();
    check("the item was NOT bought", !b.queued().some(o => o.type === "checkoff"), b.queued());
    check("it was not skipped either", !b.queued().some(o => o.type === "skip"), b.queued());
    check("and the editor opened", b.sheetOpen());
  }

  console.log("\n--- 3. the row still works the way it always did ---------------------");
  {
    const b = boot();
    const li = b.rows()[0];
    b.fire(li, "pointerdown"); b.fire(li, "pointerup"); b.fire(li, "click");
    await settle();
    check("a tap on the row still checks off", b.queued().some(o => o.type === "checkoff"), b.queued());
    check("and does not open the editor", !b.sheetOpen());
  }

  console.log("\n--- 4. the trace says which way in was used --------------------------");
  {
    const b = boot();
    b.fire(b.rows()[0].querySelector(".edit"), "click");
    await settle();
    const t = b.trace();
    check("named in the trace", /edit-button/.test(t), t);
    check("alongside proof the sheet is on screen", /shown:flex/.test(t), t);
  }

  console.log(`\n================ ${passed} passed, ${failed} failed ================`);
  process.exit(failed === 0 ? 0 : 1);
})();
