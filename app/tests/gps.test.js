/**
 * gps.test.js — "I'm at…" guessed from where the phone is.
 *
 * Setting the store by hand is the step nobody remembers in a car park, and a
 * list sorted for the wrong shop reads exactly like one sorted right. So the
 * nearest pinned store is OFFERED from the phone's position — never set
 * silently: a wrong "I'm at" stamps every check-off with the wrong shop.
 *
 * Loads the real app/index.html; the geolocation API is stubbed to answer
 * with a chosen position.
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
const settle = () => new Promise(r => setTimeout(r, 30));

const WF = { id: 1, name: "Whole Foods Market", notes: "", osm_id: "way/1", address: "", lat: 40.3081, lon: -74.6685,
             brand: "", chain: "wholefoods", chain_store_id: "10187" };
const SR = { id: 2, name: "ShopRite of Lawrenceville", notes: "", osm_id: "way/2", address: "", lat: 40.2870, lon: -74.7000,
             brand: "", chain: "shoprite", chain_store_id: "500" };
const BYNAME = { id: 3, name: "Corner Veg", notes: "", osm_id: null, address: "", lat: null, lon: null,
                 brand: "", chain: "", chain_store_id: "" };

function boot({ at = null, position = null, denied = false } = {}) {
  const dom = new JSDOM(HTML, { runScripts: "outside-only", url: "https://s.ts.net/" });
  const w = dom.window;
  w.localStorage.setItem("pc_name", "tester");
  w.localStorage.setItem("pc_base", JSON.stringify(
    { revision: 1, items: [], stores: [WF, SR, BYNAME], picks: {}, suggestions: [], away_pending: 0 }));
  if (at) w.localStorage.setItem("pc_at", JSON.stringify({ name: at, at: Date.now() }));
  w.fetch = () => Promise.resolve({ ok: true, status: 200, json: async () => ({}), text: async () => "" });
  w.WebSocket = function () { this.close = () => {}; };
  const asked = [];
  Object.defineProperty(w.navigator, "geolocation", {
    configurable: true,
    value: { getCurrentPosition: (ok, err) => {
      asked.push(1);
      if (denied) { err({ code: 1 }); return; }
      if (position) ok({ coords: { latitude: position[0], longitude: position[1] } });
    } },
  });
  w.eval(SCRIPT);
  return { w, doc: w.document, asked,
           banner: () => w.document.getElementById("gpsguess"),
           text: () => w.document.getElementById("gpsguess-text").textContent,
           at: () => { try { return JSON.parse(w.localStorage.getItem("pc_at") || "null"); } catch (e) { return null; } } };
}

(async () => {
  console.log("\n--- 1. standing in the car park, the store is offered, not set ---");
  {
    const b = boot({ position: [40.3083, -74.6683] });      // 30 m from the Whole Foods pin
    await settle();
    check("the phone was asked where it is", b.asked.length >= 1, b.asked.length);
    check("the nearest pinned store is offered", b.banner().classList.contains("on") && /Whole Foods Market/.test(b.text()), b.text());
    check("but nothing is set yet", b.at() === null, b.at());
    b.doc.getElementById("gpsguess-yes").click();
    await settle();
    check("Yes sets it", b.at() && b.at().name === "Whole Foods Market", b.at());
    check("and the offer goes away", !b.banner().classList.contains("on"));
  }

  console.log("\n--- 2. 'Not here' is an answer, remembered for the session ---------");
  {
    const b = boot({ position: [40.3083, -74.6683] });
    await settle();
    b.doc.getElementById("gpsguess-no").click();
    check("nothing set", b.at() === null, b.at());
    check("banner gone", !b.banner().classList.contains("on"));
    b.w.guessStoreByGps();
    await settle();
    check("not asked again for the same shop this session", !b.banner().classList.contains("on"));
  }

  console.log("\n--- 3. nowhere near a pinned store: no guess at all ----------------");
  {
    const b = boot({ position: [40.35, -74.66] });          // Princeton centre, 5 km from either
    await settle();
    check("no banner", !b.banner().classList.contains("on"), b.text());
    check("a store with no pin can never be guessed", !/Corner Veg/.test(b.text()));
  }

  console.log("\n--- 4. a store already chosen is left alone; a refusal is quiet ---");
  {
    const b = boot({ at: "ShopRite of Lawrenceville", position: [40.3083, -74.6683] });
    await settle();
    check("the phone is not even asked when a store is set", b.asked.length === 0, b.asked.length);
    check("and the choice stands", b.at().name === "ShopRite of Lawrenceville");
  }
  {
    const b = boot({ denied: true });
    await settle();
    check("permission refused: no banner, no error on screen", !b.banner().classList.contains("on"));
  }

  console.log(`\n================ ${passed} passed, ${failed} failed ================`);
  process.exit(failed === 0 ? 0 : 1);
})();
