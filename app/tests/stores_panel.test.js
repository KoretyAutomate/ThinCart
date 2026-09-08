/**
 * stores_panel.test.js — the Stores panel's refresh, and what survives it.
 *
 * WHY THIS FILE EXISTS. The owner reported "I somehow cannot delete the
 * stores". The server was never the problem: `apply_store_delete` is correct
 * and covered in tests/test_stores.py, and the op reached it. The panel simply
 * never redrew.
 *
 * render() live-refreshes the open Stores panel, guarded so it cannot yank a
 * half-typed note out from under someone. The guard was "anything inside the
 * panel has focus" — and on Android, tapping a <button> focuses it. So the
 * Delete button held focus from the tap onward, every subsequent render was
 * suppressed, and the row stayed on screen after the row was gone from the
 * database. Closing and reopening the panel showed it deleted all along, which
 * is why it read as "sometimes it works".
 *
 * A guard against losing typed text should protect text, not every element that
 * can take focus. These tests drive the real paths — a click on the real button
 * for the delete, a WebSocket frame for the other phone — because the bug was
 * in exactly the wiring that a more convenient stub would have skipped.
 *
 * The panel is also rebuilt while a price link is in flight, so the second half
 * of this file covers what has to live outside the DOM to survive that.
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
const settle = () => new Promise(r => setTimeout(r, 40));

const store = (id, name) => ({
  id, name, notes: "", osm_id: null, address: "", lat: null, lon: null,
  brand: "", chain: "", chain_store_id: "",
});
const stateOf = (stores, revision = 1) =>
  ({ revision, items: [], stores, picks: {}, suggestions: [], away_pending: 0 });

/**
 * Boot the real page with the Stores panel open, against a server that
 * actually applies the delete — so /api/state afterwards reflects it, the way
 * the DGX would.
 */
async function boot(stores, { linkReply = null, routes = {} } = {}) {
  const dom = new JSDOM(HTML, { runScripts: "outside-only", url: "https://s.ts.net/" });
  const w = dom.window;
  w.localStorage.setItem("pc_name", "tester");
  w.localStorage.setItem("pc_base", JSON.stringify(stateOf(stores)));

  const sent = [];
  let live = stores.slice();
  let rev = 1;
  w.fetch = (url, opts) => {
    const u = String(url);
    if (u.includes("/api/op")) {
      const op = JSON.parse(opts.body);
      sent.push(op);
      if (op.type === "store_delete") { live = live.filter(s => s.id !== op.store_id); rev++; }
      return Promise.resolve({ ok: true, status: 200, json: async () => ({ result: {} }) });
    }
    if (u.includes("/api/stores/link"))
      // A real link is a round trip to the chain and can take seconds. Held
      // open on purpose, because the window it opens is the whole point.
      return linkReply ? linkReply.promise
                       : Promise.resolve({ ok: true, status: 200, json: async () => ({}) });
    if (u.includes("/api/state"))
      return Promise.resolve({ ok: true, status: 200, json: async () => stateOf(live, rev) });
    for (const [frag, body] of Object.entries(routes))
      if (u.includes(frag)) return Promise.resolve({ ok: true, status: 200, json: async () => body });
    return Promise.resolve({ ok: true, status: 200, json: async () => ({}), text: async () => "" });
  };
  w.confirm = () => true;
  const sockets = [];
  w.WebSocket = function () { sockets.push(this); this.readyState = 1; this.close = () => {}; };
  w.eval(SCRIPT);

  await settle();                       // let the startup flush/resync finish
  const doc = w.document;
  doc.getElementById("stores-btn").click();

  return {
    w, doc, sent,
    rows: () => [...doc.querySelectorAll("#store-rows .storerow b")].map(e => e.textContent),
    delBtn: () => doc.querySelector("#store-rows .storerow .del"),
    notes: () => doc.querySelector("#store-rows .storerow textarea"),
    linkBtn: () => doc.querySelector("#store-rows .linkbtn"),
    linkText: () => {
      const el = doc.querySelector("#store-rows .storerow .addr:last-of-type");
      return el ? el.textContent : "";
    },
    // What the other phone's change looks like from in here: a frame on the
    // live socket, which is the path that actually calls render().
    push: (s) => sockets[sockets.length - 1].onmessage({ data: JSON.stringify(stateOf(s, ++rev)) }),
  };
}

(async () => {
  console.log("\n--- 1. deleting a store removes it from the panel -----------------");
  {
    const b = await boot([store(1, "Wegmans"), store(2, "Corner Veg")]);
    check("both stores are listed", b.rows().length === 2, b.rows());

    // A tap on Android focuses the button it lands on. That is the whole bug:
    // the guard read a focused Delete button as "someone is typing in here".
    const del = b.delBtn();
    del.focus();
    check("the tap leaves focus on Delete, inside the panel",
      b.doc.activeElement === del, b.doc.activeElement && b.doc.activeElement.className);

    del.click();
    await settle();

    check("the op reached the server",
      b.sent.some(o => o.type === "store_delete" && o.store_id === 1), b.sent);
    check("and the row is gone from the panel", b.rows().length === 1, b.rows());
    check("the right one went", b.rows()[0] === "🏬 Corner Veg", b.rows());
  }

  console.log("\n--- 2. a half-typed note is still never yanked away ---------------");
  {
    /* The guard exists for a real reason and has to keep working: the other
     * phone can push state at any moment, and rebuilding the panel mid-sentence
     * would lose what was typed. Text is the thing worth protecting. */
    const b = await boot([store(1, "Wegmans"), store(2, "Corner Veg")]);
    const ta = b.notes();
    ta.focus();
    ta.value = "ask about the good tomatoes";
    b.push([store(2, "Corner Veg")]);          // other phone deletes Wegmans
    await settle();
    check("the panel was NOT rebuilt under the typing", b.rows().length === 2, b.rows());
    check("and the text survives", b.notes().value === "ask about the good tomatoes",
      b.notes().value);
  }

  console.log("\n--- 3. the new-store and search fields are protected too ----------");
  {
    for (const id of ["newstore-name", "storesearch-area"]) {
      const b = await boot([store(1, "Wegmans")]);
      const inp = b.doc.getElementById(id);
      inp.focus(); inp.value = "half typed";
      b.push([]);
      await settle();
      check(`${id}: panel not rebuilt`, b.rows().length === 1, b.rows());
      check(`${id}: what was typed survives`, b.doc.getElementById(id).value === "half typed",
        b.doc.getElementById(id).value);
    }
  }

  console.log("\n--- 4. with nothing focused, the panel tracks the server ----------");
  {
    const b = await boot([store(1, "Wegmans")]);
    b.push([]);                                 // the other phone deleted it
    await settle();
    check("the other phone's delete shows up here", b.rows().length === 0, b.rows());
  }

  console.log("\n--- 5. a focused button never blocks the refresh again ------------");
  {
    /* The regression itself, stated directly: any button in the panel can hold
     * focus after a tap, so no button may suppress the redraw. */
    const b = await boot([store(1, "Wegmans")]);
    b.doc.getElementById("newstore-add").focus();
    b.push([]);
    await settle();
    check("a focused ＋ button does not freeze the panel", b.rows().length === 0, b.rows());
  }

  console.log("\n--- 6. a rebuild mid-link cannot lose the answer or double-fire --");
  {
    /* Linking asks the chain for this branch's number — seconds, over the
     * internet. The other phone can push state in that window and rebuild the
     * whole panel. The handler used to write its result into the element it had
     * captured, which by then could be detached: the reason went somewhere
     * nobody could see, and the rebuilt button came back enabled, so the same
     * lookup could be sent twice. */
    let resolve;
    const linkReply = { promise: new Promise(r => { resolve = r; }) };
    const b = await boot([store(1, "Wegmans")], { linkReply });

    b.linkBtn().click();
    await settle();
    check("the button goes away while the lookup is out", !b.linkBtn(), b.linkText());
    check("and it says so", /Linking/.test(b.linkText()), b.linkText());

    b.push([store(1, "Wegmans")]);            // the other phone rebuilds the panel
    await settle();
    check("a rebuild does not hand back a button to press again", !b.linkBtn(), b.linkText());
    check("the panel still says the lookup is running", /Linking/.test(b.linkText()), b.linkText());

    resolve({ ok: true, status: 200, json: async () => ({ reason: "not in their index" }) });
    await settle();
    check("the reason is on screen after all that",
      /not in their index/.test(b.linkText()), b.linkText());
    check("and the button is back to try again", !!b.linkBtn(), b.linkText());
  }

  console.log("\n--- 7. the failure reason outlives a later rebuild ----------------");
  {
    let resolve;
    const linkReply = { promise: new Promise(r => { resolve = r; }) };
    const b = await boot([store(1, "Wegmans")], { linkReply });
    b.linkBtn().click();
    await settle();
    resolve({ ok: true, status: 200, json: async () => ({ reason: "not in their index" }) });
    await settle();
    b.push([store(1, "Wegmans")]);
    await settle();
    check("still there after the panel is rebuilt",
      /not in their index/.test(b.linkText()), b.linkText());
  }

  console.log("\n--- 8. a successful link does not offer itself again -------------");
  {
    /* enqueue() is optimistic: it returns as soon as the op is queued, and
     * base.stores shows no link until the upsert round-trips. Clearing the
     * pending flag on that alone brought the button straight back, enabled —
     * and offline, where the op can sit queued for hours, it would offer to
     * repeat the same lookup every time the panel was drawn. */
    let resolve;
    const linkReply = { promise: new Promise(r => { resolve = r; }) };
    const b = await boot([store(1, "Wegmans")], { linkReply });
    b.linkBtn().click();
    await settle();
    resolve({ ok: true, status: 200,
              json: async () => ({ chain: "wegmans", chain_store_id: "93",
                                   address: "Princeton, NJ 08540", lat: 40.31, lon: -74.67 }) });
    await settle();

    check("the row reads as linked straight away", /93/.test(b.linkText()), b.linkText());
    // A store added by name — OpenStreetMap had never heard of it — has no
    // address of its own. The chain's directory supplied one with the link, and
    // the upsert must carry it: that is what turns a bare name into a pin.
    const up = b.sent.find(o => o.type === "store_upsert" && o.store_chain_id === "93");
    check("the chain's address and coordinates ride along on the upsert",
      up && up.store_address === "Princeton, NJ 08540" && up.store_lat === 40.31, up);
    check("with nothing to press again", !b.linkBtn(), b.linkText());

    // The server has still not caught up — the op is queued, not applied.
    b.push([store(1, "Wegmans")]);
    await settle();
    check("a rebuild before the op lands keeps it linked", /93/.test(b.linkText()), b.linkText());
    check("and still offers no second lookup", !b.linkBtn(), b.linkText());

    // Now the upsert is applied and comes back in server state.
    const linkedRow = { ...store(1, "Wegmans"), chain: "wegmans", chain_store_id: "93" };
    b.push([linkedRow]);
    await settle();
    check("the server's own value takes over", /93/.test(b.linkText()), b.linkText());
  }

  console.log("\n--- 8b. a branch the chain found is confirmed before it is pinned --");
  {
    /* A store added by name was never pointed at, and a town can hold more
     * than one branch. The chain's answer is put in front of the person; "no"
     * is an answer, not a failure, and nothing is written. */
    let resolve;
    const linkReply = { promise: new Promise(r => { resolve = r; }) };
    const b = await boot([store(1, "Whole Foods Montgomery")], { linkReply });
    const asked = [];
    b.w.confirm = (msg) => { asked.push(msg); return false; };
    b.linkBtn().click();
    await settle();
    resolve({ ok: true, status: 200, json: async () => ({ chain: "wholefoods", chain_store_id: "10738",
              address: "Skillman, NJ 08558", confirm: "Skillman, NJ 08558", lat: 40.4, lon: -74.65 }) });
    await settle();
    check("the person was asked, with the branch the chain named",
      asked.length === 1 && /Skillman, NJ 08558/.test(asked[0]), asked);
    check("declined: nothing was written", !b.sent.some(o => o.type === "store_upsert" && o.store_chain_id), b.sent);
    check("and the button is back, with a way forward", !!b.linkBtn() && /map/.test(b.linkText()), b.linkText());
  }

  console.log("\n--- 8c. a pasted link becomes a pinned, priced store in one tap ----");
  {
    /* The Montgomery Whole Foods: OpenStreetMap has never heard of it, so the
     * pin search cannot return it. A Google Maps link or the chain's own store
     * page names it exactly, and the server hands back one hit with the price
     * link already resolved. Same card, same tap, as an OSM result. */
    const hit = { name: "Whole Foods Market", address: "1200, State Road, Montgomery Township, NJ, 08558",
                  lat: 40.402224, lon: -74.652613, town: "Montgomery Township", brand: "Whole Foods",
                  osm_id: "geo:40.402224,-74.652613", chain: "wholefoods", chain_store_id: "10738", link_reason: "" };
    const b = await boot([], { routes: { "/api/stores/from_link": { result: hit, reason: "" } } });
    b.doc.getElementById("storelink").value = "https://maps.app.goo.gl/AbCdEf";
    b.doc.getElementById("storelink-go").click();
    await settle();
    const card = b.doc.querySelector("#storesearch-results .osmhit");
    check("one card, the shop the link names", card && /Whole Foods Market/.test(card.textContent), card && card.textContent);
    check("and it says prices are already linked", card && /store 10738/.test(card.textContent), card && card.textContent);
    card.click();
    await settle();
    const up = b.sent.find(o => o.type === "store_upsert");
    check("one tap adds it pinned AND linked",
      up && up.store_osm_id === "geo:40.402224,-74.652613" && up.store_lat === 40.402224
        && up.store_chain === "wholefoods" && up.store_chain_id === "10738", up);
    check("the link box is cleared", b.doc.getElementById("storelink").value === "");
  }
  {
    const b = await boot([], { routes: { "/api/stores/from_link": { result: null, reason: "not a link this can read — a Google Maps place link, or a chain's store page" } } });
    b.doc.getElementById("storelink").value = "https://example.com/x";
    b.doc.getElementById("storelink-go").click();
    await settle();
    const note = b.doc.querySelector("#storesearch-results").textContent;
    check("a link that names no shop says so, and what would work", /Google Maps/.test(note), note);
    check("and nothing was added", !b.sent.some(o => o.type === "store_upsert"), b.sent);
  }

  console.log("\n--- 9. a link completing mid-sentence does not eat the note -------");
  {
    /* The link handler redraws when the lookup returns. Calling renderStores()
     * directly there walked straight past the typing guard: start writing a
     * note while the request is out — which is exactly when you would, since it
     * is slow — and the answer arriving would rebuild the field and take the
     * text with it. Every redraw goes through refreshStores() for this reason. */
    let resolve;
    const linkReply = { promise: new Promise(r => { resolve = r; }) };
    const b = await boot([store(1, "Wegmans")], { linkReply });
    b.linkBtn().click();
    await settle();

    const ta = b.notes();
    ta.focus();
    ta.value = "the good tomatoes are by the door";
    resolve({ ok: true, status: 200,
              json: async () => ({ chain: "wegmans", chain_store_id: "93" }) });
    await settle();

    check("what was typed is still there",
      b.notes().value === "the good tomatoes are by the door", b.notes().value);

    // The redraw was skipped, not lost: the answer lives in linkOpt, so the
    // next render paints it. Move focus off the note and push state.
    ta.blur();
    b.push([store(1, "Wegmans")]);
    await settle();
    check("the link shows up once the field is no longer being typed in",
      /93/.test(b.linkText()), b.linkText());
  }

  console.log(`\n================ ${passed} passed, ${failed} failed ================`);
  process.exit(failed === 0 ? 0 : 1);
})();
