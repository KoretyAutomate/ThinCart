/**
 * longpress.test.js — the editor opens on a held finger, whatever Android sends.
 *
 * WHY THIS FILE EXISTS. Long-press has now been "fixed" twice on a theory about
 * which event the WebView emits — first a 600 ms timer (works in a browser,
 * not on the phone), then a `contextmenu` listener (a guess about Chromium).
 * The owner reported it broken both times. So the rule stopped being "identify
 * the signal" and became "accept every signal that means a held finger, and
 * record what actually arrived".
 *
 * Three ways in, and they must all reach the editor exactly once:
 *   - the timer, where nothing interrupts (browsers, iOS);
 *   - `contextmenu`, which Chromium raises for the same gesture;
 *   - a `pointercancel` on a finger that has NOT moved after a third of a
 *     second — the platform taking the gesture over, which is what a
 *     long-press IS on a WebView that emits no contextmenu.
 *
 * And the thing that must not regress: a scroll also cancels the pointer, and
 * must never be read as a long-press.
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
    { revision: 1, items: [item(1, "milk")], stores: [], picks: {}, suggestions: [], away_pending: 0 }));
  const sent = [];
  w.fetch = (url, opts) => {
    if (String(url).includes("/api/op")) sent.push(JSON.parse(opts.body));
    return Promise.resolve({ ok: true, status: 200, json: async () => ({ result: {} }), text: async () => "" });
  };
  w.WebSocket = function () { this.close = () => {}; };
  w.eval(SCRIPT);
  const doc = w.document;
  const li = doc.querySelector("#list li.item");

  /** One touch, driven event by event the way a device would. */
  const ev = (type, opts = {}) => {
    const e = new w.Event(type, { bubbles: true, cancelable: true });
    Object.assign(e, { clientX: opts.x || 0, clientY: opts.y || 0, pointerId: 1 });
    li.dispatchEvent(e);
    return e;
  };
  // What the gesture DID, read off the op queue rather than the network: the
  // queue is written synchronously, the flush behind it is not, and this is a
  // test about gestures rather than about sync.
  const queued = () => {
    try { return JSON.parse(w.localStorage.getItem("pc_queue") || "[]"); } catch (e) { return []; }
  };
  /** An event on any element — the sheet's backdrop is not the item, and that
   *  distinction is the whole of the bug this file now covers. */
  const clickOn = (el, type = "click") => {
    const e = new w.Event(type, { bubbles: true, cancelable: true });
    Object.assign(e, { clientX: 0, clientY: 0, pointerId: 1 });
    el.dispatchEvent(e);
    return e;
  };
  return { w, doc, li, sent, ev, queued, clickOn,
           sheetOpen: () => doc.getElementById("sheet").style.display === "flex",
           trace: () => { doc.getElementById("set-btn").click();
                          return doc.getElementById("diag-out").textContent; } };
}

(async () => {
  console.log("\n--- 1. the timer path: nothing interrupts the hold -----------------");
  {
    const b = boot();
    b.ev("pointerdown", { x: 50, y: 50 });
    check("the sheet is not open on touch-down", !b.sheetOpen());
    await new Promise(r => setTimeout(r, 650));
    check("a 600 ms hold opens the editor", b.sheetOpen());
    check("and the item was not checked off", !b.queued().some(o => o.type === "checkoff"), b.queued());
  }

  console.log("\n--- 2. contextmenu: what Chromium raises for the same gesture -------");
  {
    const b = boot();
    b.ev("pointerdown", { x: 50, y: 50 });
    const e = b.ev("contextmenu");
    check("the editor opens at once", b.sheetOpen());
    check("and the browser's own menu is suppressed", e.defaultPrevented);
  }

  console.log("\n--- 3. pointercancel on a still finger IS the long press ------------");
  {
    /* The case both earlier fixes missed: the WebView takes the gesture over,
     * cancels the pointer stream, and raises no contextmenu at all. */
    const b = boot();
    b.ev("pointerdown", { x: 50, y: 50 });
    await new Promise(r => setTimeout(r, 400));       // held, unmoved
    b.ev("pointercancel");
    check("the editor opens", b.sheetOpen());
  }

  console.log("\n--- 4. a scroll cancels too, and must NOT open the editor -----------");
  {
    const b = boot();
    b.ev("pointerdown", { x: 50, y: 50 });
    b.ev("pointermove", { x: 52, y: 90 });            // dragged: this is a scroll
    await new Promise(r => setTimeout(r, 400));
    b.ev("pointercancel");
    check("no editor", !b.sheetOpen());
  }
  {
    const b = boot();
    b.ev("pointerdown", { x: 50, y: 50 });
    b.ev("pointercancel");                            // cancelled immediately: a fling
    check("an instant cancel is not a hold either", !b.sheetOpen());
  }
  {
    /* The case elapsed time alone gets wrong, and the reason the test is
     * stillness: pause, THEN start to scroll. Rows are touch-action: pan-y, so
     * the native pan can cancel the pointer before the movement passes the
     * swipe threshold — a cancel, late, with barely any travel, that is
     * nonetheless a scroll. Raised by the pre-push reviewer, 2026-09-08. */
    const b = boot();
    b.ev("pointerdown", { x: 50, y: 50 });
    await new Promise(r => setTimeout(r, 400));       // a pause before scrolling
    b.ev("pointermove", { x: 50, y: 60 });            // 10 px: under the swipe threshold
    b.ev("pointercancel");                            // the pan takes over
    check("a pause and then a scroll is still a scroll", !b.sheetOpen());
    check("and the trace says why", /still\d+ms/.test(b.trace()), b.trace());
  }
  {
    /* And the same scroll delivered a pixel at a time. Measuring displacement
     * from the first point missed this entirely: it never grew by enough to
     * count as movement, so a creeping pan read as a still finger. Travel from
     * the previous point is what catches it. Reviewer, 2026-09-08. */
    const b = boot();
    b.ev("pointerdown", { x: 50, y: 50 });
    await new Promise(r => setTimeout(r, 400));
    for (let i = 1; i <= 7; i++) b.ev("pointermove", { x: 50, y: 50 + i });
    b.ev("pointercancel");
    check("a scroll that creeps a pixel at a time is still a scroll", !b.sheetOpen(), b.trace());
  }
  {
    /* Movement back toward the start is movement too, and shrinks displacement. */
    const b = boot();
    b.ev("pointerdown", { x: 50, y: 50 });
    await new Promise(r => setTimeout(r, 400));
    b.ev("pointermove", { x: 50, y: 56 });
    b.ev("pointermove", { x: 50, y: 50 });            // back where it began
    b.ev("pointercancel");
    check("a finger that moved away and back has still moved", !b.sheetOpen(), b.trace());
  }
  {
    /* The thing that must survive all of that: a real hold, whose hardware
     * reports sub-pixel jitter, still opens the editor. */
    const b = boot();
    b.ev("pointerdown", { x: 50, y: 50 });
    b.ev("pointermove", { x: 50.4, y: 50.3 });
    await new Promise(r => setTimeout(r, 400));
    b.ev("pointercancel");
    check("a still finger that jitters sub-pixel is a hold", b.sheetOpen(), b.trace());
  }

  console.log("\n--- 5. it opens ONCE, and a tap still checks off --------------------");
  {
    const b = boot();
    b.ev("pointerdown", { x: 50, y: 50 });
    b.ev("contextmenu");
    await new Promise(r => setTimeout(r, 650));       // the timer would fire here
    b.ev("pointerup"); b.ev("click");
    check("the editor is open", b.sheetOpen());
    check("the hold never becomes a purchase", !b.queued().some(o => o.type === "checkoff"), b.queued());
  }
  {
    const b = boot();
    b.ev("pointerdown", { x: 50, y: 50 });
    b.ev("pointerup"); b.ev("click");
    check("a quick tap still checks the item off", b.queued().some(o => o.type === "checkoff"), b.queued());
    check("and does not open the editor", !b.sheetOpen());
  }

  console.log("\n--- 5b. the finger that opened the editor must not shut it ---------");
  {
    /* THE bug, from the phone's own trace:
     *     down → ctx@592ms → OPEN:ctx → up@2083ms/4px
     * The editor opened correctly and there is no `click` on the item, because
     * by then the sheet covered it. The finger came down on a row, the sheet
     * appeared underneath it, and lifting produced a click on the backdrop —
     * which dismissed the editor a moment after it opened. Four rounds of
     * event fixes were all correct; none of them was the problem.
     *
     * Note the 1.5 s between opening and the release: a time-based grace
     * would not have covered it. */
    const b = boot();
    b.ev("pointerdown", { x: 50, y: 50 });
    b.ev("contextmenu");
    check("the editor opened", b.sheetOpen());
    await new Promise(r => setTimeout(r, 50));
    b.ev("pointerup");
    b.clickOn(b.doc.getElementById("sheet"));      // the release lands on the backdrop
    check("and the finger lifting does NOT close it", b.sheetOpen());
  }
  {
    // ...while a deliberate tap on the dark area still dismisses it.
    const b = boot();
    b.ev("pointerdown", { x: 50, y: 50 });
    b.ev("contextmenu");
    const sheet = b.doc.getElementById("sheet");
    b.clickOn(sheet, "pointerdown");               // a fresh press, on the backdrop
    b.clickOn(sheet);
    check("a tap that begins on the backdrop still dismisses", !b.sheetOpen());
  }
  {
    // ...and a drag that starts inside the card never dismisses an edit.
    const b = boot();
    b.ev("pointerdown", { x: 50, y: 50 });
    b.ev("contextmenu");
    const sheet = b.doc.getElementById("sheet");
    b.clickOn(sheet.querySelector(".card"), "pointerdown");
    b.clickOn(sheet);
    check("a drag out of the card does not dismiss", b.sheetOpen());
  }

  console.log("\n--- 6. what happened is recorded, for the phone that disagrees ------");
  {
    const b = boot();
    b.ev("pointerdown", { x: 50, y: 50 });
    await new Promise(r => setTimeout(r, 400));
    b.ev("pointercancel");
    const t = b.trace();
    check("the trace names the events and which one opened it",
      /down/.test(t) && /cancel@\d+ms/.test(t) && /OPEN:cancel/.test(t), t);
  }

  console.log(`\n================ ${passed} passed, ${failed} failed ================`);
  process.exit(failed === 0 ? 0 : 1);
})();
