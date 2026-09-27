/**
 * organic_brand_price.test.js — Phase 7 on the phone (PLAN.md §Phase 7).
 *
 * - The editor's 🌱 toggle and Brand field send exactly the edit op fields the
 *   server reads, and only when they changed.
 * - A row shows 🌱 and the brand.
 * - A queued "organic milk" lands on the Milk row (organic) instead of drawing
 *   a second Milk — the phone mirrors the server's split.
 * - 💲 By price groups by the server's cheapest store, asks only for items the
 *   server knows, says why an item has no price, and never saves anything.
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

function item(id, name, extra = {}) {
  return { id: `i${id}`, catalog_id: id, name, name_en: null, category: "dairy", emoji: "",
           note: "", budget: null, qty_note: "", added_by: "t", organic: false, brand: "",
           added_at: "2026-09-01T00:00:00+00:00", store: null, store_source: null, ...extra };
}

const STORES = [{ id: 7, name: "Wegmans", chain: "wegmans", chain_store_id: "93" },
                { id: 8, name: "Whole Foods", chain: "wholefoods", chain_store_id: "10738" }];

function boot({ items, queue = [], where = null, stores = STORES, fetchImpl = null }) {
  const dom = new JSDOM(HTML, { runScripts: "outside-only", url: "https://s.ts.net/" });
  const w = dom.window;
  w.localStorage.setItem("pc_name", "tester");
  const state = { revision: 1, items, stores, picks: {}, suggestions: [], away_pending: 0 };
  w.localStorage.setItem("pc_base", JSON.stringify(state));
  w.localStorage.setItem("pc_queue", JSON.stringify(queue));
  const calls = [];
  w.fetch = (url, opts) => {
    calls.push({ url: String(url), body: opts && opts.body ? JSON.parse(opts.body) : null });
    if (String(url) === "/api/op") return new Promise(() => {});      // queue stays queued
    if (String(url).startsWith("/api/state"))                           // a resync sees the same list
      return Promise.resolve({ ok: true, status: 200, json: async () => state });
    if (String(url) === "/api/where")
      return Promise.resolve({ ok: true, status: 200, json: async () => where });
    return Promise.resolve({ ok: true, status: 200, json: async () => ({}), text: async () => "" });
  };
  if (fetchImpl) w.fetch = fetchImpl;                 // a test that drives the network itself
  w.WebSocket = function () { this.close = () => {}; };
  w.eval(SCRIPT);
  const doc = w.document;
  return { w, doc, calls,
           rows: () => [...doc.querySelectorAll("#list li.item")],
           // what the phone has queued or sent: the queue is where an edit lands first
           ops: () => {
             let q = [];
             try { q = JSON.parse(w.localStorage.getItem("pc_queue") || "[]"); } catch (e) { /* none */ }
             return [...q, ...calls.filter(c => c.url === "/api/op").map(c => c.body)];
           } };
}

function openEditor(b, i) {
  b.rows()[i].querySelector(".edit").dispatchEvent(new b.w.Event("click", { bubbles: true }));
}

(async () => {
  console.log("\n--- 1. the editor sends organic and brand, only when changed --------");
  {
    const b = boot({ items: [item(1, "milk")] });
    await settle();
    openEditor(b, 0); await settle();
    check("toggle starts off", b.doc.getElementById("sheet-organic").checked === false);
    b.doc.getElementById("sheet-organic").checked = true;
    b.doc.getElementById("sheet-brand").value = "  Horizon ";
    b.doc.getElementById("sheet-save").click();
    await settle();
    const edit = b.ops().find(o => o.type === "edit");
    check("an edit op went out", !!edit, b.ops());
    check("with organic and a trimmed brand", edit && edit.organic === true && edit.brand === "Horizon", edit);

    const c = boot({ items: [item(1, "milk", { organic: true, brand: "Horizon" })] });
    await settle();
    openEditor(c, 0); await settle();
    check("the sheet shows what is saved",
      c.doc.getElementById("sheet-organic").checked && c.doc.getElementById("sheet-brand").value === "Horizon");
    c.doc.getElementById("sheet-save").click();
    await settle();
    check("nothing changed, nothing sent", !c.ops().some(o => o.type === "edit"), c.ops());
  }

  console.log("\n--- 2. a row says organic and names the brand ----------------------");
  {
    const b = boot({ items: [item(1, "milk", { organic: true, brand: "Horizon" })] });
    await settle();
    const text = b.rows()[0].textContent;
    check("🌱 on the row", /milk 🌱/.test(text), text);
    check("the brand in the sub-line", /🏷 Horizon/.test(text), text);
  }

  console.log("\n--- 3. a queued 'organic milk' is the Milk row, not a second one ---");
  {
    const q = [{ op_id: "q1", type: "add", name: "Organic Milk", item_id: "new1", actor: "tester" }];
    const b = boot({ items: [item(1, "milk")], queue: q });
    await settle();
    check("still one row", b.rows().length === 1, b.rows().map(r => r.textContent));
    check("and it is organic now", /🌱/.test(b.rows()[0].textContent), b.rows()[0].textContent);
    const c = boot({ items: [], queue: [{ op_id: "q2", type: "add", name: "organic kale", item_id: "n2", actor: "t" }] });
    await settle();
    check("a new one is drawn under its base name", /kale 🌱/.test(c.rows()[0].textContent), c.rows()[0].textContent);
  }

  console.log("\n--- 4. 💲 By price groups by the cheapest store and saves nothing --");
  {
    const where = { partial: false, items: {
      "1": { cheapest: { store: "Whole Foods", amount: 3.49, unit_price: "$0.05/fl oz", product: "365 Organic Milk",
                         exact: false, fetched_at: new Date().toISOString() }, quotes: [{}], comparable: true },
      "2": { cheapest: null, comparable: false, reason: null, quotes: [
               { store: "Wegmans", amount: 3.99, unit_price: "$0.25/oz" },
               { store: "Whole Foods", amount: 6.29, unit_price: "$6.29/count" }] },
      "3": { cheapest: null, quotes: [], comparable: false, reason: "unasked" },
    } };
    const b = boot({ items: [item(1, "milk", { store: "Wegmans", store_source: "history" }), item(2, "bread"),
                             item(3, "eggs")],
                     queue: [{ op_id: "q3", type: "add", name: "tea", item_id: "n3", actor: "t" }], where });
    await settle();
    b.doc.getElementById("stores-btn").click();
    b.doc.getElementById("plan-byprice").click();
    await settle(); await settle();
    const req = b.calls.find(c => c.url === "/api/where");
    check("asked for the server's items only (not the queued tea)",
      req && JSON.stringify(req.body.catalog_ids) === "[1,2,3]", req && req.body);
    const groups = [...b.doc.querySelectorAll("#plan-groups .plangroup")].map(g => g.textContent);
    const wf = groups.find(g => /Whole Foods/.test(g)) || "";
    check("milk moved to its cheapest store", /milk/.test(wf) && /\$3\.49/.test(wf) && /best match/.test(wf), groups);
    const all = groups.join("|");
    check("different units are not called cheapest", /not ranked/.test(all), all);
    check("the prices found are still shown, unranked",
      /Wegmans \$3\.99 \(\$0\.25\/oz\)/.test(all) && /Whole Foods \$6\.29/.test(all), all);
    check("an unreachable store is named as the reason", /unreachable/.test(all), all);
    check("a queued item says it is not synced", /not synced/.test(all), all);
    check("nothing was saved", !b.ops().some(o => o.type === "edit"), b.ops());
    b.doc.getElementById("plan-byprice").click();
    await settle();
    const back = [...b.doc.querySelectorAll("#plan-groups .plangroup")].map(g => g.textContent).join("|");
    check("toggling back restores the usual grouping", /Wegmans/.test(back) && !/\$3\.49/.test(back), back);
  }

  console.log("\n--- 4b. an unsynced preference edit is not priced on stale wishes -");
  {
    const where = { partial: false, items: {} };
    const b = boot({ items: [item(1, "milk"), item(2, "bread")], where,
                     queue: [{ op_id: "e1", type: "edit", item_id: "i1", catalog_id: 1, organic: true, actor: "t" }] });
    await settle();
    b.doc.getElementById("stores-btn").click();
    b.doc.getElementById("plan-byprice").click();
    await settle(); await settle();
    const req = b.calls.find(c => c.url === "/api/where");
    check("the item with a queued 🌱 edit is not asked about",
      req && JSON.stringify(req.body.catalog_ids) === "[2]", req && req.body);
    const all = [...b.doc.querySelectorAll("#plan-groups .plangroup")].map(g => g.textContent).join("|");
    check("and says it is not synced yet", /milk 🌱[^|]*not synced/.test(all), all);
  }

  console.log("\n--- 4c. a queued 'organic milk' on a listed milk is unsettled too --");
  {
    const b = boot({ items: [item(1, "milk"), item(2, "bread")], where: { partial: false, items: {} },
                     queue: [{ op_id: "a1", type: "add", name: "organic milk", item_id: "n9", actor: "t" }] });
    await settle();
    b.doc.getElementById("stores-btn").click();
    b.doc.getElementById("plan-byprice").click();
    await settle(); await settle();
    const req = b.calls.find(c => c.url === "/api/where");
    check("milk is not priced on its old conventional wish",
      req && JSON.stringify(req.body.catalog_ids) === "[2]", req && req.body);
  }

  console.log("\n--- 4d. an older answer never overwrites a newer one ---------------");
  {
    let release = null;
    const b = boot({ items: [item(1, "milk")], where: null });
    const answers = [];
    b.w.fetch = (url, opts) => {
      if (String(url) !== "/api/where")
        return Promise.resolve({ ok: true, status: 200, json: async () => ({}), text: async () => "" });
      const n = answers.length;
      const body = { partial: false, items: { "1": { cheapest: { store: n === 0 ? "OLD" : "NEW", amount: 1,
        unit_price: "$0.10/oz", product: "p", exact: true, fetched_at: "" }, quotes: [{}], comparable: true } } };
      answers.push(body);
      if (n === 0) return new Promise(r => { release = () => r({ ok: true, status: 200, json: async () => body }); });
      return Promise.resolve({ ok: true, status: 200, json: async () => body });
    };
    await settle();
    b.doc.getElementById("stores-btn").click();
    b.doc.getElementById("plan-byprice").click();     // first ask: held
    await settle();
    b.doc.getElementById("stores-btn").click();       // reopen: second ask answers at once
    await settle(); await settle();
    release();                                        // the first answer arrives late
    await settle(); await settle();
    const all = [...b.doc.querySelectorAll("#plan-groups .plangroup")].map(g => g.textContent).join("|");
    check("the newer answer stands", /NEW/.test(all) && !/OLD/.test(all), all);
  }

  console.log("\n--- 4e. once the edit is acknowledged, its item is priced ---------");
  {
    // Codex review: the server's revision can arrive BEFORE the op's ACK, so a
    // refresh keyed on the revision alone never asked about the item again.
    const items = [item(1, "milk"), item(2, "bread")];
    let ack = null;
    const asked = [];
    const state = { revision: 1, items, stores: STORES, picks: {}, suggestions: [], away_pending: 0 };
    const fetchImpl = (url, opts) => {
      const u = String(url);
      if (u === "/api/op") return new Promise(r => { ack = () => r({ ok: true, status: 200,
        json: async () => ({ result: { edited: "i1", changed: true } }) }); });
      if (u.startsWith("/api/state")) return Promise.resolve({ ok: true, status: 200, json: async () => state });
      if (u === "/api/where") {
        asked.push(JSON.parse(opts.body).catalog_ids);
        return Promise.resolve({ ok: true, status: 200, json: async () => ({ partial: false, items: {} }) });
      }
      return Promise.resolve({ ok: true, status: 200, json: async () => ({}), text: async () => "" });
    };
    // the startup flush sends the edit; its ACK is held until ack()
    const b = boot({ items, fetchImpl,
                     queue: [{ op_id: "e2", type: "edit", item_id: "i1", catalog_id: 1, organic: true, actor: "t" }] });
    await settle();
    b.doc.getElementById("stores-btn").click();
    b.doc.getElementById("plan-byprice").click();
    await settle(); await settle();
    check("while unacknowledged, milk is held back", JSON.stringify(asked[0]) === "[2]", asked);
    ack();                                            // same revision comes back with the resync
    await settle(); await settle(); await settle();
    check("after the ACK the list is asked about again, milk included",
      asked.some(a => JSON.stringify(a) === "[1,2]"), asked);
  }

  console.log("\n--- 5. no priced store: say what to do ------------------------------");
  {
    const b = boot({ items: [item(1, "milk")], stores: [] });
    await settle();
    b.doc.getElementById("stores-btn").click();
    b.doc.getElementById("plan-byprice").click();
    await settle();
    const note = b.doc.getElementById("plan-price-note").textContent;
    check("explains how to get prices", /pin a Wegmans/.test(note), note);
    check("and did not ask the server", !b.calls.some(c => c.url === "/api/where"));
  }

  console.log(`\n================ ${passed} passed, ${failed} failed ================`);
  process.exit(failed === 0 ? 0 : 1);
})();
