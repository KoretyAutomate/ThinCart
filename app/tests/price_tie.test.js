/**
 * price_tie.test.js — stores tied at the lowest price are all shown, and the
 * item stays on 📍 at every one of them (owner, 2026-10-10).
 * Run: cd app && npm test
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
const mkStore = (id, name) => ({ id, name, notes: "", osm_id: null, address: "", lat: null, lon: null,
                                 brand: "", chain: "", chain_store_id: "" });

function item(id, name, store, price) {
  return { id: `i${id}`, catalog_id: id, name, name_en: null, category: "pantry", emoji: "", note: "", budget: null,
           qty_note: "", added_by: "t", brand: "", added_at: "2026-09-01T00:00:00+00:00",
           store, store_source: store ? "price" : null, price: price || null };
}
const PRICE = (store, also) => ({ store, also, amount: 4, product: "x", unit_label: "$0.25/oz", total_label: "",
                                  exact: true, computed_at: "", fetched_at: "", organic_fallback: false, partial: false });

function boot(items, at) {
  const dom = new JSDOM(HTML, { runScripts: "outside-only", url: "https://s.ts.net/" });
  const w = dom.window;
  const state = { revision: 1, items, stores: [mkStore(1, "Wegmans"), mkStore(2, "ShopRite")], picks: {},
                  suggestions: [], away_pending: 0, plants: { count: 1, target: 30 } };
  w.localStorage.setItem("pc_name", "tester");
  w.localStorage.setItem("pc_base", JSON.stringify(state));
  w.localStorage.setItem("pc_at", JSON.stringify({ name: at, at: Date.now() }));
  w.localStorage.setItem("pc_here", "1");
  w.fetch = (url) => String(url).startsWith("/api/state")
    ? Promise.resolve({ ok: true, status: 200, json: async () => state })
    : Promise.resolve({ ok: false, status: 404, json: async () => ({}) });
  w.WebSocket = function () { this.close = () => {}; };
  w.eval(SCRIPT);
  return { w, doc: w.document,
           rows: () => [...w.document.querySelectorAll("#list li.item .nm1")].map(e => e.textContent.trim().split(" ").pop()) };
}

(async () => {
  const items = [item(1, "rice", "Wegmans", PRICE("Wegmans", ["ShopRite"])),
                 item(2, "milk", "Wegmans", PRICE("Wegmans", [])),
                 item(3, "salt", "ShopRite", PRICE("ShopRite", []))];
  const b = boot(items, "ShopRite");
  await drain();
  check("at ShopRite, the item tied there is on the list", b.rows().includes("rice"), b.rows());
  check("an item only cheaper at Wegmans stays hidden", !b.rows().includes("milk"), b.rows());
  check("an item cheapest here is shown", b.rows().includes("salt"), b.rows());
  const c = boot([item(1, "rice", "Wegmans", { ...PRICE("Wegmans", ["ShopRite"]) })].map(i => ({ ...i, store_source: "preferred" })), "ShopRite");
  await drain();
  check("the owner's own pick is not overridden by a tie", c.rows().length === 0, c.rows());
  console.log(`\n================ ${passed} passed, ${failed} failed ================`);
  if (failed) process.exit(1);
})();
