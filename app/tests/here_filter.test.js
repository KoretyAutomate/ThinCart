/**
 * here_filter.test.js — 📍 "this store only", and groups folded at the top
 * (the owner's ask, 2026-10-04).
 *
 * - The 🌱 plant count leaves the header; Plants & ideas is opened from ⚙️.
 * - 📍 appears only while "I'm at" a store. On, the list keeps what to buy
 *   THERE, hides items for other stores (and says how many), and folds the
 *   items with no store yet into one group at the top.
 * - In aisle order, "Aisle unknown" / "Not looked up" are on top and folded;
 *   tapping a heading opens or folds it, and the choice is remembered.
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

const WEGMANS = { id: 1, name: "Wegmans", notes: "", osm_id: null, address: "", lat: null, lon: null,
                  brand: "", chain: "wegmans", chain_store_id: "93" };
const SHOPRITE = { id: 2, name: "ShopRite", notes: "", osm_id: null, address: "", lat: null, lon: null,
                   brand: "", chain: "", chain_store_id: "" };

function item(id, name, store = null, category = "pantry") {
  return { id: `i${id}`, catalog_id: id, name, name_en: null, category, emoji: "", note: "", budget: null,
           qty_note: "", added_by: "t", brand: "", added_at: "2026-09-01T00:00:00+00:00",
           store, store_source: store ? "preferred" : null };
}

function boot({ items, at = null, view = "cat", here = false, aisles = null, plants = { count: 12, target: 30 } }) {
  const dom = new JSDOM(HTML, { runScripts: "outside-only", url: "https://s.ts.net/" });
  const w = dom.window;
  const state = { revision: 1, items, stores: [WEGMANS, SHOPRITE], picks: {}, suggestions: [],
                  away_pending: 0, plants };
  w.localStorage.setItem("pc_name", "tester");
  w.localStorage.setItem("pc_base", JSON.stringify(state));
  w.localStorage.setItem("pc_view", view);
  if (at) w.localStorage.setItem("pc_at", JSON.stringify({ name: at, at: Date.now() }));
  if (here) w.localStorage.setItem("pc_here", "1");
  w.fetch = (url) => {
    const u = String(url);
    if (u.startsWith("/api/state")) return Promise.resolve({ ok: true, status: 200, json: async () => state });
    if (u.startsWith("/api/aisles") && aisles)
      return Promise.resolve({ ok: true, status: 200, json: async () => aisles });
    return Promise.resolve({ ok: false, status: 404, json: async () => ({}) });
  };
  w.WebSocket = function () { this.close = () => {}; };
  w.eval(SCRIPT);
  const doc = w.document;
  return { w, doc,
           btn: () => doc.getElementById("here-btn"),
           groups: () => [...doc.querySelectorAll("#list .cat")].map(e => e.textContent),
           rows: () => [...doc.querySelectorAll("#list li.item .nm1")].map(e => e.textContent.trim()),
           note: () => doc.getElementById("herenote").textContent };
}

(async () => {
  console.log("\n--- 1. the plant count is out of the header ---------------------");
  {
    const b = boot({ items: [item(1, "rice")] });
    await drain();
    const header = b.doc.querySelector("body > header").textContent;
    check("no 🌱 count in the header", !/🌱/.test(header) && !/12\/30/.test(header), header);
    const p = b.doc.getElementById("plants");
    check("Plants & ideas lives in ⚙️", !!p.closest("#set-panel") && /Plants/.test(p.textContent), p.textContent);
    b.doc.getElementById("set-panel").style.display = "flex";
    p.click();
    check("it opens the plants panel and closes ⚙️",
      b.doc.getElementById("ideas").style.display === "flex"
      && b.doc.getElementById("set-panel").style.display === "none");
  }

  console.log("\n--- 2. 📍 exists only while at a store --------------------------");
  {
    const b = boot({ items: [item(1, "rice")] });
    await drain();
    check("hidden when not at a store", !b.btn().classList.contains("at"));
    const c = boot({ items: [item(1, "rice")], at: "ShopRite" });
    await drain();
    check("shown at a store, off by default",
      c.btn().classList.contains("at") && !c.btn().classList.contains("on"));
  }

  console.log("\n--- 3. 📍 on: what to buy here; no-store folded on top ----------");
  {
    const items = [item(1, "rice", "ShopRite"), item(2, "milk", "Wegmans"), item(3, "tofu"),
                   item(4, "salt", "ShopRite"), item(5, "kale")];
    const b = boot({ items, at: "ShopRite" });
    await drain();
    check("off: the whole list", b.rows().length === 5, b.rows());
    b.btn().click();
    await drain();
    check("the button is lit", b.btn().classList.contains("on") && b.btn().getAttribute("aria-pressed") === "true");
    check("items for another store are hidden, and it says so",
      !b.rows().some(r => r.endsWith("milk")) && /ShopRite/.test(b.note()) && /1/.test(b.note()), [b.rows(), b.note()]);
    const g = b.groups();
    check("no-store items are one folded group at the top, with their count",
      /^▸ 🛒 Anywhere/.test(g[0]) && g[0].endsWith("2"), g);
    check("folded: its items are not drawn", b.rows().map(r => r.split(" ").pop()).join(",") === "rice,salt", b.rows());
    b.doc.querySelector("#list .cat.fold").click();
    await drain();
    check("tapping the heading opens it, on top",
      /^▾/.test(b.groups()[0]) && b.rows().map(r => r.split(" ").pop()).join(",") === "tofu,kale,rice,salt",
      [b.groups(), b.rows()]);
    check("the open/folded choice is remembered",
      JSON.parse(b.w.localStorage.getItem("pc_folded")).indexOf("zzx-nostore") === -1);
    b.btn().click();
    await drain();
    check("📍 off again: everything back, no note", b.rows().length === 5 && b.note() === "", b.note());
  }
  {
    // leaving the store ("— none —") makes 📍 meaningless: the full list returns
    const b = boot({ items: [item(1, "rice", "ShopRite"), item(2, "milk", "Wegmans")], here: true });
    await drain();
    check("remembered 📍 with no store set filters nothing",
      b.rows().length === 2 && !b.btn().classList.contains("at"), b.rows());
  }

  console.log("\n--- 4. aisle order: unknown folded on top ------------------------");
  {
    const items = [item(1, "bread", "Wegmans", "bakery"), item(2, "rice", "Wegmans"), item(3, "milk", "Wegmans")];
    const b = boot({ items, at: "Wegmans", view: "aisle", aisles: {
      aisles: { "1": { label: "Aisle 2", aisle: "2", exact: true } }, store: "Wegmans", partial: true, unasked: ["2"],
    } });
    for (let i = 0; i < 4; i++) await drain();
    const g = b.groups();
    check("unknown and not-looked-up come before the aisles, folded",
      g.length === 3 && /^▸ 🤷 Aisle unknown/.test(g[0]) && /^▸ ⏳/.test(g[1]) && g[2] === "Aisle 2", g);
    check("only the aisle's item is drawn", b.rows().map(r => r.split(" ").pop()).join(",") === "bread", b.rows());
    b.doc.querySelectorAll("#list .cat.fold")[0].click();
    await drain();
    check("opening unknown shows its item first",
      b.rows().map(r => r.split(" ").pop()).join(",") === "milk,bread", b.rows());
  }

  console.log(`\n================ ${passed} passed, ${failed} failed ================`);
  if (failed) process.exit(1);
})();
