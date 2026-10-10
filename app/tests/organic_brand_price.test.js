/**
 * organic_brand_price.test.js — Phase 7 on the phone (PLAN.md §Phase 7).
 *
 * - 🌱 Organic is ONE household setting in ⚙️ (the owner's call, 2026-09-27):
 *   the switch sends a settings op, and By price waits until the server has it.
 * - The editor's Brand field sends exactly the edit op field the server reads,
 *   and only when it changed; the row shows the brand.
 * - A queued "organic milk" lands on the Milk row instead of drawing a second
 *   Milk — the phone mirrors the server's split.
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
           note: "", budget: null, qty_note: "", added_by: "t", brand: "",
           added_at: "2026-09-01T00:00:00+00:00", store: null, store_source: null, ...extra };
}

const STORES = [{ id: 7, name: "Wegmans", chain: "wegmans", chain_store_id: "93" },
                { id: 8, name: "Whole Foods", chain: "wholefoods", chain_store_id: "10738" }];

function boot({ items, queue = [], where = null, stores = STORES, fetchImpl = null, settings = { organic: false },
               extra = {} }) {
  const dom = new JSDOM(HTML, { runScripts: "outside-only", url: "https://s.ts.net/" });
  const w = dom.window;
  w.localStorage.setItem("pc_name", "tester");
  const state = { revision: 1, items, stores, settings, picks: {}, suggestions: [], away_pending: 0, ...extra };
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
  const sockets = [];
  w.WebSocket = function () { this.close = () => {}; sockets.push(this); };
  w.eval(SCRIPT);
  const doc = w.document;
  // what the other phone, or the server's enrichment, pushes over the socket
  const push = next => sockets[sockets.length - 1].onmessage({ data: JSON.stringify(next) });
  return { w, doc, calls, push,
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
  console.log("\n--- 1. 🍃 is one household switch, a button in the header ---------");
  {
    const b = boot({ items: [item(1, "milk")] });
    await settle();
    check("no per-item organic toggle in the editor", b.doc.getElementById("sheet-organic") === null);
    const btn = b.doc.getElementById("org-btn");
    check("the button is in the header and starts off",
      btn && btn.closest("header") && !btn.classList.contains("on") && btn.getAttribute("aria-pressed") === "false");
    btn.click();
    await settle();
    const op = b.ops().find(o => o.type === "settings");
    check("tapping it sends a settings op", op && op.organic === true, b.ops());
    check("and lights up at once", btn.classList.contains("on") && /ON/.test(btn.textContent), btn.textContent);
    check("with a toast that says what it means", /Organic ON/.test(b.doc.getElementById("toastmsg").textContent));
    const c = boot({ items: [item(1, "milk")], settings: { organic: true } });
    await settle();
    check("the button shows the server's setting", c.doc.getElementById("org-btn").classList.contains("on"));
  }

  console.log("\n--- 1a. ✈️ Travel lives in ⚙️ now; ⚙️ says when trips wait ---------");
  {
    const b = boot({ items: [item(1, "milk")] });
    await settle();
    const trips = b.doc.getElementById("trips");
    check("the Travel button is inside Settings, not the header",
      trips && b.doc.getElementById("set-panel").contains(trips) && !trips.closest("header"));
    const c = boot({ items: [item(1, "milk")], extra: { away_pending: 3 } });
    await settle();
    check("⚙️ turns amber when trips wait", c.doc.getElementById("set-btn").classList.contains("pending"));
    check("and the Travel button says how many", /3 to review/.test(c.doc.getElementById("trips").textContent),
      c.doc.getElementById("trips").textContent);
  }

  console.log("\n--- 1c. a suggestion tap adds, and is traced — never opens cycles -");
  {
    const b = boot({ items: [item(1, "milk")], extra: { suggestions: [
      { catalog_id: 9, name: "tea", name_en: "tea", tier: "high", weeks: 1 }] } });
    await settle();
    const chip = b.doc.querySelector("#chips .chip:not(.more)");
    check("the suggestion is shown", !!chip);
    chip.click();
    await settle();
    check("it was added", b.ops().some(o => o.type === "add" && o.name === "tea"), b.ops());
    check("Purchase cycles did not open", b.doc.getElementById("cyc").style.display !== "flex");
    b.doc.getElementById("set-btn").click();
    const trace = b.doc.getElementById("diag-out").textContent;
    check("the tap is in the ⚙️ trace", /chip:click/.test(trace), trace);
  }

  console.log("\n--- 1b. the phone splits 'organic X' exactly as the server does -----");
  {
    const cases = JSON.parse(fs.readFileSync(
      path.join(__dirname, "..", "..", "tests", "fixtures", "split_organic_cases.json"), "utf8"));
    const b = boot({ items: [] });
    for (const [typed, base, organic] of cases) {
      const got = b.w.splitOrganic(typed);
      check(`splitOrganic(${JSON.stringify(typed)})`, got[0] === base && got[1] === organic, got);
    }
  }

  console.log("\n--- 2. the editor sends the brand, only when changed ----------------");
  {
    const b = boot({ items: [item(1, "milk")] });
    await settle();
    openEditor(b, 0); await settle();
    b.doc.getElementById("sheet-brand").value = "  Horizon ";
    b.doc.getElementById("sheet-save").click();
    await settle();
    const edit = b.ops().find(o => o.type === "edit");
    check("an edit op with the trimmed brand", edit && edit.brand === "Horizon" && !("organic" in edit), edit);

    const c = boot({ items: [item(1, "milk", { brand: "Horizon" })] });
    await settle();
    openEditor(c, 0); await settle();
    check("the sheet shows the saved brand", c.doc.getElementById("sheet-brand").value === "Horizon");
    c.doc.getElementById("sheet-save").click();
    await settle();
    check("nothing changed, nothing sent", !c.ops().some(o => o.type === "edit"), c.ops());
    const text = c.rows()[0].textContent;
    check("the row names the brand, and carries no per-item 🌱", /🏷 Horizon/.test(text) && !/🌱/.test(text), text);
  }

  console.log("\n--- 3. a queued 'organic milk' is the Milk row, not a second one ---");
  {
    const q = [{ op_id: "q1", type: "add", name: "Organic Milk", item_id: "new1", actor: "tester" }];
    const b = boot({ items: [item(1, "milk")], queue: q });
    await settle();
    check("still one row", b.rows().length === 1, b.rows().map(r => r.textContent));
    const c = boot({ items: [], queue: [{ op_id: "q2", type: "add", name: "organic kale", item_id: "n2", actor: "t" }] });
    await settle();
    check("a new one is drawn under its base name", /kale/.test(c.rows()[0].textContent)
      && !/organic/i.test(c.rows()[0].textContent), c.rows()[0].textContent);
  }

  console.log("\n--- 4. 💲 By price groups by the cheapest store and saves nothing --");
  {
    const where = { partial: false, items: {
      "1": { cheapest: { store: "Whole Foods", amount: 3.49, unit_price: "$0.05/fl oz", product: "365 Organic Milk",
                         exact: false, fetched_at: new Date().toISOString() }, quotes: [{}], comparable: true,
             stores: { "7": "conflict", "8": "ok" } },
      "2": { cheapest: null, comparable: false, reason: null, quotes: [
               { store: "Wegmans", amount: 3.99, unit_price: "$0.25/oz", product: "Wegmans Milk",
                 pack_size: "16 oz", exact: false },
               { store: "Whole Foods", amount: 6.29, unit_price: "$6.29/count", product: "365 Milk",
                 pack_size: "64 fl oz", exact: true }] },
      "3": { cheapest: null, quotes: [], comparable: false, reason: "unasked" },
      "4": { cheapest: { store_id: 7, store: "Wegmans", amount: 4, unit_label: "$0.33/oz", product: "Syrup 12 oz",
                         exact: false, fetched_at: "" }, comparable: true,
             quotes: [{ store_id: 7, store: "Wegmans", amount: 4, unit_label: "$0.33/oz", product: "Syrup 12 oz" },
                      { store_id: 8, store: "Whole Foods", amount: 3, product: "Syrup", pack_size: "12 fl oz" }] },
    } };
    const b = boot({ items: [item(1, "milk", { store: "Wegmans", store_source: "history" }), item(2, "bread"),
                             item(3, "eggs"), item(4, "syrup")],
                     queue: [{ op_id: "q3", type: "add", name: "tea", item_id: "n3", actor: "t" }], where });
    await settle();
    b.doc.getElementById("stores-btn").click();
    b.doc.getElementById("plan-byprice").click();
    await settle(); await settle();
    const req = b.calls.find(c => c.url === "/api/where");
    check("asked for the server's items only (not the queued tea)",
      req && JSON.stringify(req.body.catalog_ids) === "[1,2,3,4]", req && req.body);
    const groups = [...b.doc.querySelectorAll("#plan-groups .plangroup")].map(g => g.textContent);
    const wf = groups.find(g => g.startsWith("🏬 Whole Foods")) || "";   // the group headed Whole Foods
    check("milk moved to its cheapest store", /milk/.test(wf) && /\$3\.49/.test(wf) && /best match/.test(wf), groups);
    check("and the store left out says why (Codex review 2026-09-28)",
      /⚠ Wegmans: your chosen product no longer matches/.test(wf), wf);
    const all = groups.join("|");
    check("different units are not called cheapest", /not ranked/.test(all), all);
    check("the prices found are still shown, unranked, with product and size",
      /Wegmans \$3\.99 Wegmans Milk 16 oz \$0\.25\/oz \(best match\)/.test(all)
      && /Whole Foods \$6\.29 365 Milk 64 fl oz/.test(all), all);
    check("an unreachable store is named as the reason", /unreachable/.test(all), all);
    check("a product that cannot be compared is still shown beside the winner (Codex review)",
      /not comparable: Whole Foods \$3\.00 Syrup 12 fl oz/.test(all), all);
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
                     queue: [{ op_id: "e1", type: "edit", item_id: "i1", catalog_id: 1, brand: "Horizon", actor: "t" }] });
    await settle();
    b.doc.getElementById("stores-btn").click();
    b.doc.getElementById("plan-byprice").click();
    await settle(); await settle();
    const req = b.calls.find(c => c.url === "/api/where");
    check("the item with a queued brand edit is not asked about",
      req && JSON.stringify(req.body.catalog_ids) === "[2]", req && req.body);
    const all = [...b.doc.querySelectorAll("#plan-groups .plangroup")].map(g => g.textContent).join("|");
    check("and says it is not synced yet", /milk[^|]*not synced/.test(all), all);
  }

  console.log("\n--- 4c. a queued 🌱 change holds the whole ask back ----------------");
  {
    const b = boot({ items: [item(1, "milk")], where: { partial: false, items: {} },
                     queue: [{ op_id: "s1", type: "settings", organic: true, actor: "t" }] });
    await settle();
    b.doc.getElementById("stores-btn").click();
    b.doc.getElementById("plan-byprice").click();
    await settle(); await settle();
    check("nothing is asked while the server has the old setting",
      !b.calls.some(c => c.url === "/api/where"), b.calls.map(c => c.url));
    check("and the panel says why", /Waiting for the 🌱 setting/.test(b.doc.getElementById("plan-price-note").textContent),
      b.doc.getElementById("plan-price-note").textContent);
    const c = boot({ items: [item(1, "milk")], settings: { organic: true },
                     where: { partial: false, items: { "1": { cheapest: null, comparable: false, reason: null,
                       organic_fallback: true, quotes: [{ store: "Wegmans", amount: 9, unit_price: "" }] } } } });
    await settle();
    c.doc.getElementById("stores-btn").click();
    c.doc.getElementById("plan-byprice").click();
    await settle(); await settle();
    const note = c.doc.getElementById("plan-price-note").textContent;
    const all = [...c.doc.querySelectorAll("#plan-groups .plangroup")].map(g => g.textContent).join("|");
    check("with 🍃 on, the note says organic for food", /🍃 Organic for food/.test(note), note);
    check("an item nobody sells organic says it fell back", /no organic found/.test(all), all);
  }

  console.log("\n--- 4d. an older answer never overwrites a newer one ---------------");
  {
    let release = null;
    const b = boot({ items: [item(1, "milk")], where: null });
    const answers = [];
    b.w.fetch = (url, opts) => {
      if (String(url).startsWith("/api/state"))     // the post-check resync sees the same list
        return Promise.resolve({ ok: true, status: 200, json: async () => (
          { revision: 1, items: [item(1, "milk")], stores: STORES, settings: { organic: false }, picks: {},
            suggestions: [], away_pending: 0 }) });
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
                     queue: [{ op_id: "e2", type: "edit", item_id: "i1", catalog_id: 1, brand: "Horizon", actor: "t" }] });
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

  console.log("\n--- 4f. the answer is tied to its input (Codex code review, round 3)");
  {
    // one fetch that counts asks and can hold the next answer
    const mk = (items, stores = STORES) => {
      const t = { asked: [], hold: false, release: null,
        state: { revision: 1, items, stores, picks: {}, suggestions: [], away_pending: 0 },
        answer: () => ({ partial: false, items: {
        "1": { cheapest: { store: "Wegmans", amount: 3, unit_price: "$0.03/fl oz", product: "Milk",
               exact: false, fetched_at: "" }, quotes: [{}], comparable: true } } }) };
      t.fetch = (url, opts) => {
        const u = String(url);
        if (u === "/api/where") {
          t.asked.push(JSON.parse(opts.body).catalog_ids);
          const body = t.answer();
          const ok = { ok: true, status: 200, json: async () => body };
          if (t.hold) return new Promise(r => { t.release = () => r(ok); });
          return Promise.resolve(ok);
        }
        if (u === "/api/op") return new Promise(() => {});
        if (u.startsWith("/api/state")) return Promise.resolve({ ok: true, status: 200, json: async () => t.state });
        return Promise.resolve({ ok: true, status: 200, json: async () => ({}), text: async () => "" });
      };
      t.push = (b, next) => { t.state = next; b.push(next); };
      return t;
    };
    const st = (items, stores = STORES, revision = 1) =>
      ({ revision, items, stores, picks: {}, suggestions: [], away_pending: 0 });

    // (a) enrichment names an item in English WITHOUT a revision bump: ask again
    let t = mk([item(1, "牛乳")]);
    let b = boot({ items: t.state.items, fetchImpl: t.fetch });
    await settle();
    b.doc.getElementById("stores-btn").click(); b.doc.getElementById("plan-byprice").click();
    await settle(); await settle();
    t.push(b, st([item(1, "牛乳", { name_en: "milk" })]));
    await settle(); await settle();
    check("(a) a new English name is a new question", t.asked.length === 2, t.asked);

    // (b) the other phone sets a brand: the old answer is not shown meanwhile
    t = mk([item(1, "milk")]);
    b = boot({ items: t.state.items, fetchImpl: t.fetch });
    await settle();
    b.doc.getElementById("stores-btn").click(); b.doc.getElementById("plan-byprice").click();
    await settle(); await settle();
    t.hold = true;
    t.push(b, st([item(1, "milk", { brand: "Horizon" })]));
    await settle();
    const during = b.doc.getElementById("plan-price-note").textContent
      + [...b.doc.querySelectorAll("#plan-groups .plangroup")].map(g => g.textContent).join("|");
    check("(b) while re-asking, the old answer is not shown", /Checking prices/.test(during) && !/\$3\.00/.test(during), during);
    t.release(); await settle(); await settle();

    // (b2) the other phone changes the pick at a SECOND chain: a new question
    t = mk([item(1, "milk")]);
    t.state.picks_by_chain = { "1": { wholefoods: "WF1", wegmans: "W1" } };
    b = boot({ items: t.state.items, fetchImpl: t.fetch });
    await settle();
    b.doc.getElementById("stores-btn").click(); b.doc.getElementById("plan-byprice").click();
    await settle(); await settle();
    const before = t.asked.length;
    t.push(b, { ...st([item(1, "milk")]), picks_by_chain: { "1": { wholefoods: "WF1", wegmans: "W2" } } });
    await settle(); await settle();
    check("(b2) a changed pick at another chain re-asks", t.asked.length === before + 1, t.asked);

    // (b3) the server's price question changes with nothing the phone can see
    t = mk([item(1, "milk", { price_key: "k1" })]);
    b = boot({ items: t.state.items, fetchImpl: t.fetch });
    await settle();
    b.doc.getElementById("stores-btn").click(); b.doc.getElementById("plan-byprice").click();
    await settle(); await settle();
    const n0 = t.asked.length;
    t.push(b, st([item(1, "milk", { price_key: "k2" })]));
    await settle(); await settle();
    check("(b3) a new server-side question re-asks", t.asked.length === n0 + 1, t.asked);

    // (c) price mode on before any store is linked; linking one asks
    t = mk([item(1, "milk")], []);
    b = boot({ items: t.state.items, stores: [], fetchImpl: t.fetch });
    await settle();
    b.doc.getElementById("stores-btn").click(); b.doc.getElementById("plan-byprice").click();
    await settle();
    check("(c) no store yet: nothing asked", t.asked.length === 0, t.asked);
    t.push(b, st([item(1, "milk")], STORES, 2));
    await settle(); await settle();
    check("(c) the first linked store triggers an ask", t.asked.length === 1, t.asked);

    // (d) a half-typed store note survives an answer arriving
    t = mk([item(1, "milk")]); t.hold = true;
    b = boot({ items: t.state.items, fetchImpl: t.fetch });
    await settle();
    b.doc.getElementById("stores-btn").click(); b.doc.getElementById("plan-byprice").click();
    await settle();
    const ta = b.doc.querySelector("#store-rows textarea");
    ta.focus(); ta.value = "half-typed note";
    t.release(); await settle(); await settle();
    const now = b.doc.querySelector("#store-rows textarea");
    check("(d) the unsaved note is still there", now && now.value === "half-typed note", now && now.value);
    const plan = [...b.doc.querySelectorAll("#plan-groups .plangroup")].map(g => g.textContent).join("|");
    check("(d) and the price answer is shown anyway", /\$3\.00/.test(plan)
      && !/Checking prices/.test(b.doc.getElementById("plan-price-note").textContent), plan);
  }

  console.log("\n--- 4g. a failed ask is retried on request, not remembered --------");
  {
    let n = 0;
    const fetchImpl = (url) => {
      const u = String(url);
      if (u === "/api/where") { n++; return Promise.reject(new TypeError("offline")); }
      if (u === "/api/op") return new Promise(() => {});
      if (u.startsWith("/api/state")) return Promise.resolve({ ok: true, status: 200, json: async () => (
        { revision: 1, items: [item(1, "milk")], stores: STORES, settings: { organic: false }, picks: {},
          suggestions: [], away_pending: 0 }) });
      return Promise.resolve({ ok: true, status: 200, json: async () => ({}), text: async () => "" });
    };
    const b = boot({ items: [item(1, "milk")], fetchImpl });
    await settle();
    b.doc.getElementById("stores-btn").click(); b.doc.getElementById("plan-byprice").click();
    await settle(); await settle();
    check("the first ask failed and said so", n === 1 && /Could not reach/.test(b.doc.getElementById("plan-price-note").textContent),
      [n, b.doc.getElementById("plan-price-note").textContent]);
    await settle();
    check("and did not loop", n === 1, n);
    b.doc.getElementById("plan-byprice").click(); b.doc.getElementById("plan-byprice").click();
    await settle(); await settle();
    check("switching it back on asks again", n === 2, n);
    b.w.dispatchEvent(new b.w.Event("online"));
    await settle(); await settle();
    check("coming back online asks once more", n === 3, n);
  }

  console.log("\n--- 4h. an answer ages out: waking days later asks again ----------");
  {
    // Codex review 2026-09-28: a phone left on the panel kept a days-old answer.
    let n = 0;
    const fetchImpl = (url) => {
      const u = String(url);
      if (u === "/api/where") { n++; return Promise.resolve({ ok: true, status: 200,
        json: async () => ({ partial: false, items: {} }) }); }
      if (u === "/api/op") return new Promise(() => {});
      if (u.startsWith("/api/state")) return Promise.resolve({ ok: true, status: 200, json: async () => (
        { revision: 1, items: [item(1, "milk")], stores: STORES, settings: { organic: false }, picks: {},
          suggestions: [], away_pending: 0 }) });
      return Promise.resolve({ ok: true, status: 200, json: async () => ({}), text: async () => "" });
    };
    const b = boot({ items: [item(1, "milk")], fetchImpl });
    await settle();
    b.doc.getElementById("stores-btn").click(); b.doc.getElementById("plan-byprice").click();
    await settle(); await settle();
    b.w.dispatchEvent(new b.w.Event("online"));
    await settle(); await settle();
    check("a fresh answer is not re-asked on wake", n === 1, n);
    const realNow = b.w.Date.now;
    b.w.Date.now = () => realNow() + 3 * 86400000;   // three days on the panel
    b.w.dispatchEvent(new b.w.Event("online"));
    await settle(); await settle();
    check("an old one is", n === 2, n);
    b.w.Date.now = realNow;
  }

  console.log("\n--- 8. price-first where to buy (PLAN.md Phase 8) -------------------");
  {
    const price = { store: "Whole Foods", amount: 2.5, product: "Rice 1 lb", unit_label: "$0.16/oz",
                    total_label: "2 × 1 lb = $5.00", exact: false, computed_at: new Date().toISOString() };
    const priced = item(1, "rice", { store: "Whole Foods", store_source: "price", price, buy_qty: "2 lb",
                                     buy_qty_ok: true, history_store: "Wegmans" });
    const b = boot({ items: [priced] });
    await settle();
    const chip = b.rows()[0].querySelector(".stchip");
    check("the chip says the price chose the store", chip && chip.classList.contains("price")
      && /💲 Whole Foods/.test(chip.textContent), chip && chip.textContent);
    openEditor(b, 0); await settle();
    check("the editor shows how", /2 × 1 lb = \$5\.00/.test(b.doc.getElementById("sheet-price").textContent),
      b.doc.getElementById("sheet-price").textContent);
    const opts = [...b.doc.querySelectorAll("#sheet-stores .stopt")].map(o => o.textContent);
    check("'no pick' reads as the cheapest store", opts[0] === "💲 Cheapest (Whole Foods)", opts);
    check("the amount field shows what is set", b.doc.getElementById("sheet-buyqty").value === "2 lb");
    b.doc.getElementById("sheet-buyqty").value = "3 lb";
    b.doc.getElementById("sheet-save").click();
    await settle();
    const edit = b.ops().find(o => o.type === "edit");
    check("a changed amount is sent", edit && edit.buy_qty === "3 lb", edit);
    const row = b.rows()[0].textContent;
    check("and until it syncs the old price is not shown — history stands in",
      /🕘 Wegmans/.test(row) && !/Whole Foods/.test(row), row);

    // the owner's own pick wins, and the editor shows the cheaper store beside it
    const c = boot({ items: [item(2, "milk", { store: "Wegmans", store_source: "preferred", price,
                                                history_store: null })] });
    await settle();
    openEditor(c, 0); await settle();
    check("your pick and the cheapest are both shown",
      /Cheapest: Whole Foods[^]*you chose Wegmans/.test(c.doc.getElementById("sheet-price").textContent),
      c.doc.getElementById("sheet-price").textContent);
    [...c.doc.querySelectorAll("#sheet-stores .stopt")][0].click();    // "use cheapest"
    c.doc.getElementById("sheet-save").click();
    await settle();
    const clear = c.ops().find(o => o.type === "edit");
    check("choosing the cheapest clears your pick", clear && clear.store === "", clear);
    check("and the row moves to the cheapest store at once (offline too)",
      /💲 Whole Foods/.test(c.rows()[0].textContent), c.rows()[0].textContent);

    const e = boot({ items: [item(5, "kale", { store: "Wegmans", store_source: "price",
      price: { ...price, store: "Wegmans", organic_fallback: true } })] });
    await settle();
    openEditor(e, 0); await settle();
    check("a regular-product stand-in says so in the editor (Codex review)",
      /no organic found/.test(e.doc.getElementById("sheet-price").textContent),
      e.doc.getElementById("sheet-price").textContent);

    const d = boot({ items: [item(3, "beans", { buy_qty: "a few", buy_qty_ok: false })] });
    await settle();
    openEditor(d, 0); await settle();
    check("an amount the server could not read is flagged",
      /Not understood/.test(d.doc.getElementById("sheet-buyqty-warn").textContent));
  }

  console.log("\n--- 8c. your pick holds with price details showing (Codex review) ---");
  {
    const where = { partial: false, items: {
      "1": { cheapest: { store_id: 8, store: "Whole Foods", amount: 3, unit_label: "$0.05/fl oz", product: "Milk",
                         exact: false, fetched_at: "" }, quotes: [{}], comparable: true } } };
    const b = boot({ items: [item(1, "milk", { store: "Wegmans", store_source: "preferred" })], where });
    await settle();
    b.doc.getElementById("stores-btn").click();
    b.doc.getElementById("plan-byprice").click();
    await settle(); await settle();
    const groups = [...b.doc.querySelectorAll("#plan-groups .plangroup")].map(g => g.textContent);
    const weg = groups.find(g => g.startsWith("🏬 Wegmans")) || "";
    check("the item stays under the store you chose", /milk/.test(weg), groups);
    check("and the cheaper store is named beside it", /Cheapest: Whole Foods[^]*you chose Wegmans/.test(weg), weg);
    const f = boot({ items: [item(1, "milk", { store: "Wegmans", store_source: "preferred" })],
                     where: { partial: false, items: { "1": { ...where.items["1"], organic_fallback: true } } } });
    await settle();
    f.doc.getElementById("stores-btn").click();
    f.doc.getElementById("plan-byprice").click();
    await settle(); await settle();
    const fg = [...f.doc.querySelectorAll("#plan-groups .plangroup")].map(g => g.textContent).join("|");
    check("a regular-product stand-in says so under your pick too (Codex review)", /no organic found/.test(fg), fg);
  }

  console.log("\n--- 8d. the plan and the chip agree on a partial check (Codex review)");
  {
    const price = { store: "Wegmans", amount: 2, product: "Peas", unit_label: "$0.13/oz", total_label: "",
                    exact: false, computed_at: new Date().toISOString() };
    const where = { partial: true, items: {
      "1": { cheapest: { store_id: 8, store: "Whole Foods", amount: 5, unit_label: "$0.31/oz", product: "Peas",
                         exact: false, fetched_at: "" }, quotes: [{}], comparable: true,
             stores: { "7": "unasked", "8": "ok" } } } };
    const b = boot({ items: [item(1, "peas", { store: "Wegmans", store_source: "price", price })], where });
    await settle();
    b.doc.getElementById("stores-btn").click();
    b.doc.getElementById("plan-byprice").click();
    await settle(); await settle();
    const groups = [...b.doc.querySelectorAll("#plan-groups .plangroup")].map(g => g.textContent);
    const weg = groups.find(g => g.startsWith("🏬 Wegmans")) || "";
    check("grouped where the chip says (the kept answer)", /peas/.test(weg), groups);
    check("the partial check is shown, labelled, not obeyed", /latest check[^]*Whole Foods/.test(weg), weg);
  }

  console.log("\n--- 8b. pending 🍃 / pick / amount changes hide the stored price ----");
  {
    const price = { store: "Whole Foods", amount: 2.5, product: "Rice 1 lb", unit_label: "$0.16/oz",
                    total_label: "", exact: false, computed_at: new Date().toISOString() };
    const mk = () => item(1, "rice", { store: "Whole Foods", store_source: "price", price, history_store: "Wegmans" });
    for (const [label, op] of [
      ["a queued 🍃 change", { op_id: "s9", type: "settings", organic: true, actor: "t" }],
      ["a queued product pick", { op_id: "p9", type: "product_pick", catalog_id: 1, pick_chain: "wegmans",
                                  pick_sku: "X", pick_name: "x", actor: "t" }],
      ["a queued amount", { op_id: "q9", type: "edit", item_id: "i1", catalog_id: 1, buy_qty: "2 lb", actor: "t" }],
    ]) {
      const b = boot({ items: [mk()], queue: [op] });
      await settle();
      const row = b.rows()[0].textContent;
      check(`${label}: history stands in for the price until it syncs`,
        /🕘 Wegmans/.test(row) && !/Whole Foods/.test(row), row);
    }
    // and the price details view treats an unsynced amount as unsettled
    const where = { partial: false, items: {} };
    const b = boot({ items: [mk(), item(2, "tea")], where,
                     queue: [{ op_id: "q8", type: "edit", item_id: "i1", catalog_id: 1, buy_qty: "3 lb", actor: "t" }] });
    await settle();
    b.doc.getElementById("stores-btn").click();
    b.doc.getElementById("plan-byprice").click();
    await settle(); await settle();
    const req = b.calls.find(c => c.url === "/api/where");
    check("an unsynced amount is not priced on the old one", req && JSON.stringify(req.body.catalog_ids) === "[2]",
      req && req.body);
  }

  console.log("\n--- 4i. a price check pulls the saved recommendation (Codex review) -");
  {
    const b = boot({ items: [item(1, "milk")], where: { partial: false, items: {} } });
    await settle();
    const before = b.calls.filter(c => c.url.startsWith("/api/state")).length;
    b.doc.getElementById("stores-btn").click();
    b.doc.getElementById("plan-byprice").click();
    await settle(); await settle();
    const after = b.calls.filter(c => c.url.startsWith("/api/state")).length;
    check("the list is re-fetched after the check, socket or not", after > before, [before, after]);
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
