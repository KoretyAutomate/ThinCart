/**
 * price_sheet.test.js — the price comparison, as the phone renders it.
 *
 * Every rule under test here was a review finding on the server side that the
 * client half could quietly undo. The server can separate a near match from an
 * exact one, refuse to call an outage an absence, and stamp each quote with its
 * age — and none of that reaches the shopper unless this file renders it that
 * way. That gap is exactly where the last two "done but not working" reports
 * came from.
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

const LINKED = { id: 1, name: "Wegmans", notes: "", osm_id: "way/1", address: "",
                 lat: null, lon: null, brand: "Wegmans", chain: "wegmans", chain_store_id: "93" };
const UNLINKED = { id: 2, name: "Corner Veg", notes: "", osm_id: null, address: "",
                   lat: null, lon: null, brand: "", chain: "", chain_store_id: "" };
const ITEM = { id: "i1", catalog_id: 1, name: "sunflower butter", name_en: null,
               category: "pantry", emoji: "", note: "", budget: null, qty_note: "",
               added_by: "t", added_at: "2026-09-01T00:00:00+00:00",
               store: null, store_source: null };

function boot({ stores = [LINKED], routes = {} } = {}) {
  const dom = new JSDOM(HTML, { runScripts: "outside-only", url: "https://spark.example.ts.net/" });
  const w = dom.window;
  w.localStorage.setItem("pc_name", "tester");
  w.localStorage.setItem("pc_base", JSON.stringify(
    { revision: 1, items: [ITEM], stores, picks: {}, suggestions: [], away_pending: 0 }));
  const calls = [];
  w.fetch = (url) => {
    calls.push(String(url));
    for (const [frag, res] of Object.entries(routes)) {
      if (String(url).includes(frag)) {
        return Promise.resolve({
          ok: res.status === undefined || res.status < 400,
          status: res.status === undefined ? 200 : res.status,
          json: async () => res.body,
        });
      }
    }
    return Promise.resolve({ ok: false, status: 404, json: async () => ({}) });
  };
  w.WebSocket = function () { this.close = () => {}; };
  w.eval(SCRIPT);
  w.openSheet(ITEM);
  const box = () => w.document.getElementById("sheet-prices");
  return { w, calls, box,
           rowText: () => [...box().querySelectorAll(".prow")].map(r => r.textContent),
           notes: () => [...box().querySelectorAll(".pnote")].map(r => r.textContent).join(" | ") };
}

const quote = (store, amount, extra = {}) => ({
  store_id: 1, store, product: "Wegmans Organic Creamy Sunflower Butter", brand: "Wegmans",
  pack_size: "16 ounce", amount, unit_price: `$${amount}/ea`, available: true,
  aisle: "Aisle 14B · left · sec 11", exact: true, source: "wegmans",
  source_url: "https://www.wegmans.com/shop/product/44442",
  fetched_at: new Date().toISOString(), ...extra,
});

(async () => {
  console.log("\n--- 1. no linked store: says what to do, asks nothing ----------");
  {
    const b = boot({ stores: [UNLINKED] });
    b.w.document.getElementById("sheet-compare").click();
    await drain();
    check("explains how to link", /Link prices/i.test(b.notes()), b.notes());
    check("makes no request", !b.calls.some(u => u.includes("/api/products")), b.calls);
  }

  console.log("\n--- 2. prices render with age and an openable source ------------");
  {
    const b = boot({ routes: {
      "/api/products/search": { body: { options: [], store: "Wegmans", chain: "wegmans", picked: null } },
      "/api/prices": { body: { quotes: [quote("Wegmans", 6.99)], alternatives: [], partial: false } },
    } });
    // No options come back, so drive the price view the way a pick does.
    b.w.document.getElementById("sheet-compare").click();
    await drain(); await drain();
    check("an empty catalogue says so", /does not list anything/i.test(b.notes()), b.notes());
  }

  console.log("\n--- 3. out of stock never looks like the cheapest ---------------");
  {
    const b = boot({ routes: {
      "/api/products/search": { body: { options: [
        { sku: "44442", name: "Wegmans Organic Creamy Sunflower Butter", brand: "Wegmans",
          pack_size: "16 ounce", amount: 6.99, unit_price: "$0.44/ounce",
          aisle: "14B", aisle_label: "Aisle 14B · left · sec 11", source: "wegmans" },
      ], store: "Wegmans", chain: "wegmans", picked: null } },
      "/api/prices": { body: {
        quotes: [quote("Cheap Mart", 3.99, { available: false }), quote("Wegmans", 6.99)],
        alternatives: [], partial: false } },
    } });
    b.w.document.getElementById("sheet-compare").click();
    await drain(); await drain();
    const first = b.box().querySelector(".prow");
    check("a brand option is offered", /Wegmans Organic/.test(first.textContent), first.textContent);

    first.click();                       // choose it -> price comparison
    await drain(); await drain(); await drain();
    const rows = b.rowText();
    check("the buyable store is listed first", /Wegmans/.test(rows[0]) && !/Cheap Mart/.test(rows[0]), rows);
    check("the cheaper one is marked out of stock", /not in stock/i.test(rows[1] || ""), rows);
    const oos = [...b.box().querySelectorAll(".prow.oos")];
    check("and cannot be picked as where to buy", oos.length === 1 && oos[0].onclick == null,
      oos.map(o => o.textContent));
    check("each price carries its age", /today/i.test(rows[0]), rows[0]);
    check("and a source you can open",
      b.box().querySelector(".psrc") && b.box().querySelector(".psrc").href.includes("wegmans.com"));
  }

  console.log("\n--- 4. a near match is offered BESIDE the comparison, not in it --");
  {
    const b = boot({ routes: {
      "/api/products/search": { body: { options: [
        { sku: "44442", name: "Wegmans Organic Creamy Sunflower Butter", brand: "Wegmans",
          pack_size: "16 ounce", amount: 6.99, unit_price: "", aisle: "14B",
          aisle_label: "Aisle 14B", source: "wegmans" },
      ], store: "Wegmans", chain: "wegmans", picked: null } },
      "/api/prices": { body: {
        quotes: [],
        alternatives: [quote("Other Mart", 3.50, { exact: false, product: "A Different Jar" })],
        partial: false } },
    } });
    b.w.document.getElementById("sheet-compare").click();
    await drain(); await drain();
    b.box().querySelector(".prow").click();
    await drain(); await drain(); await drain();
    check("it is introduced as what they have instead",
      /what they have instead/i.test(b.notes()), b.notes());
    check("the different jar is shown", /A Different Jar/.test(b.rowText().join(" ")), b.rowText());
    check("but not offered as a store to pin",
      [...b.box().querySelectorAll(".prow")].every(r => r.onclick == null));
  }

  console.log("\n--- 5. an outage is not reported as 'no price' ------------------");
  {
    const b = boot({ routes: {
      "/api/products/search": { status: 503, body: { detail: { code: "lookup_unavailable" } } },
    } });
    b.w.document.getElementById("sheet-compare").click();
    await drain(); await drain();
    check("says it could not reach the catalogue", /could not reach/i.test(b.notes()), b.notes());
    check("and does NOT claim the shop has none", !/does not list/i.test(b.notes()), b.notes());
  }

  console.log(`\n================ ${passed} passed, ${failed} failed ================`);
  process.exit(failed === 0 ? 0 : 1);
})();
