/**
 * aisle_price.test.js — the client half of Phase 6, in a DOM.
 *
 * WHY THIS FILE EXISTS. Twice now a feature has been reported finished on the
 * strength of a green suite and then not worked on the phone. Both times the
 * reason was the same: the tests covered a layer next to the one that runs on
 * the device. The APK's launcher was tested in jsdom while the bug sat in the
 * native navigation policy; then the server side of prices and aisles was
 * tested end to end while `app/index.html` — the file the phones actually
 * execute — had no automated coverage of any kind.
 *
 * So this loads the REAL app/index.html, seeds real state, and drives the
 * grouping, sorting and rendering the phone would do. It cannot press a button
 * on a handset, and it does not claim to. What it removes is the class of
 * failure where the served page is broken and every test is still green.
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

const STORE_WEGMANS = {
  id: 1, name: "Wegmans", notes: "", osm_id: "way/1",
  address: "29 Emmons Dr, Princeton", lat: null, lon: null, brand: "Wegmans",
  chain: "wegmans", chain_store_id: "93",
};
const STORE_PLAIN = {
  id: 2, name: "Corner Veg", notes: "", osm_id: null, address: "",
  lat: null, lon: null, brand: "", chain: "", chain_store_id: "",
};

function item(id, name, category) {
  return { id: `i${id}`, catalog_id: id, name, name_en: null, category,
           emoji: "", note: "", budget: null, qty_note: "", added_by: "t",
           added_at: "2026-09-01T00:00:00+00:00", store: null, store_source: null };
}

/**
 * Boot the real page with seeded state.
 * `routes` maps a URL substring -> {status, body}; anything unmatched 404s, so a
 * test can never accidentally depend on a request it did not think about.
 */
function boot({ items = [], stores = [], picks = {}, at = null, view = "cat", routes = {} } = {}) {
  const dom = new JSDOM(HTML, { runScripts: "outside-only", url: "https://spark.example.ts.net/" });
  const w = dom.window;
  const base = { revision: 1, items, stores, picks, suggestions: [], away_pending: 0 };

  w.localStorage.setItem("pc_name", "tester");
  w.localStorage.setItem("pc_base", JSON.stringify(base));
  w.localStorage.setItem("pc_view", view);
  if (at) w.localStorage.setItem("pc_at", JSON.stringify({ name: at, at: Date.now() }));

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

  const groups = () => [...w.document.querySelectorAll("#list .cat")].map(e => e.textContent);
  const rows = () => [...w.document.querySelectorAll("#list li.item .nm1")].map(e => e.textContent.trim());
  return { w, calls, groups, rows,
           doc: w.document,
           note: () => w.document.getElementById("aislenote").textContent };
}

const AISLES_OK = {
  aisles: {
    "1": { label: "Aisle 2 · left · sec 3", aisle: "2", exact: true },
    "2": { label: "Aisle 10 · right", aisle: "10", exact: true },
    "3": { label: "Dairy", aisle: "Dairy", exact: false },
  },
  store: "Wegmans", partial: false, unasked: [],
};

(async () => {
  console.log("\n--- 1. the toggle appears only where the store can answer -------");
  {
    const b = boot({ items: [item(1, "milk", "dairy")], stores: [STORE_PLAIN], at: "Corner Veg" });
    check("hidden for a store with no price source",
      !b.doc.getElementById("viewtoggle").classList.contains("on"));
  }
  {
    const b = boot({ items: [item(1, "milk", "dairy")], stores: [STORE_WEGMANS], at: "Wegmans" });
    check("shown for a linked store", b.doc.getElementById("viewtoggle").classList.contains("on"));
    check("category is the default", b.doc.getElementById("view-cat").classList.contains("sel"));
  }
  {
    const b = boot({ items: [item(1, "milk", "dairy")], stores: [STORE_WEGMANS] });
    check("hidden when not standing in any store",
      !b.doc.getElementById("viewtoggle").classList.contains("on"));
  }

  console.log("\n--- 2. aisle view groups in WALKING order, not alphabetical -----");
  {
    const b = boot({
      items: [item(1, "bread", "bakery"), item(2, "rice", "pantry"), item(3, "milk", "dairy")],
      stores: [STORE_WEGMANS], at: "Wegmans", view: "aisle",
      routes: { "/api/aisles": { body: AISLES_OK } },
    });
    await drain(); await drain(); await drain();
    // Aisle 2 before aisle 10 — sorted as text it would be the other way round,
    // and the whole point of the view is the order you walk it.
    check("aisle 2 comes before aisle 10",
      b.groups().join(" | ") === "Aisle 2 · left · sec 3 | Aisle 10 · right | Dairy",
      b.groups());
    // Row text carries the item's icon, so compare on the names it ends with.
    const names = b.rows().map(s => s.split(" ").pop());
    check("items follow their groups", names.join(",") === "bread,rice,milk", b.rows());
  }

  console.log("\n--- 3. unknown vs never-asked are different groups --------------");
  {
    const b = boot({
      items: [item(1, "bread", "bakery"), item(2, "rice", "pantry"), item(3, "milk", "dairy")],
      stores: [STORE_WEGMANS], at: "Wegmans", view: "aisle",
      routes: { "/api/aisles": { body: {
        aisles: { "1": { label: "Aisle 2", aisle: "2", exact: true } },
        store: "Wegmans", partial: true, unasked: ["2"],
      } } },
    });
    await drain(); await drain(); await drain();
    const g = b.groups().join(" | ");
    check("the looked-up item is in its aisle", g.includes("Aisle 2"), g);
    check("the unasked item is NOT called unknown", g.includes("Not looked up"), g);
    check("the genuinely unknown item is", g.includes("Aisle unknown"), g);
    check("unknown sorts last", g.indexOf("Aisle unknown") > g.indexOf("Not looked up"), g);
    check("and it says a lookup fell short", /could not be looked up/i.test(b.note()), b.note());
  }

  console.log("\n--- 4. a 503 says WHICH kind it is ------------------------------");
  {
    const off = boot({
      items: [item(1, "milk", "dairy")], stores: [STORE_WEGMANS], at: "Wegmans", view: "aisle",
      routes: { "/api/aisles": { status: 503, body: { detail: { code: "lookup_disabled", mode: "off" } } } },
    });
    await drain(); await drain(); await drain();
    check("disabled reads as a setting", /off on the server/i.test(off.note()), off.note());

    const down = boot({
      items: [item(1, "milk", "dairy")], stores: [STORE_WEGMANS], at: "Wegmans", view: "aisle",
      routes: { "/api/aisles": { status: 503, body: { detail: { code: "lookup_unavailable" } } } },
    });
    await drain(); await drain(); await drain();
    check("an outage does not", /could not reach/i.test(down.note()), down.note());
  }

  console.log("\n--- 5. category view is untouched by any of it ------------------");
  {
    const b = boot({
      items: [item(1, "bread", "bakery"), item(3, "milk", "dairy")],
      stores: [STORE_WEGMANS], at: "Wegmans", view: "cat",
    });
    await drain();
    check("groups by category", b.groups().length === 2, b.groups());
    check("asks for no aisles at all",
      !b.calls.some(u => u.includes("/api/aisles")), b.calls);
  }

  console.log(`\n================ ${passed} passed, ${failed} failed ================`);
  process.exit(failed === 0 ? 0 : 1);
})();
