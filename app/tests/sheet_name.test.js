/**
 * sheet_name.test.js — the edit sheet shows the name the list shows.
 *
 * Owner's report (2026-10-09): with English selected, ✎ Edit showed the item's
 * name in Japanese. The sheet filled its Name box from the stored name
 * (`it.name`, the Japanese catalog name) while the row and the sheet title use
 * the language-aware name. Now the box holds what the list shows, and Save
 * sends a rename only when that text was changed.
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

const ITEMS = [
  { id: "i1", catalog_id: 12, name: "パプリカ", name_en: "red bell pepper", category: "produce", emoji: "",
    note: "", budget: null, qty_note: "", added_by: "t", brand: "", added_at: "2026-09-01T00:00:00+00:00",
    store: null, store_source: null },
  { id: "i2", catalog_id: 40, name: "ごま油", name_en: null, category: "pantry", emoji: "",
    note: "", budget: null, qty_note: "", added_by: "t", brand: "", added_at: "2026-09-01T00:00:00+00:00",
    store: null, store_source: null },
  { id: "i3", catalog_id: 41, name: "white rice", name_en: "white rice", category: "pantry", emoji: "",
    note: "", budget: null, qty_note: "", added_by: "t", brand: "", added_at: "2026-09-01T00:00:00+00:00",
    store: null, store_source: null },
];

function boot(lang) {
  const dom = new JSDOM(HTML, { runScripts: "outside-only", url: "https://s.ts.net/" });
  const w = dom.window;
  const state = { revision: 1, items: ITEMS, stores: [], picks: {}, suggestions: [], away_pending: 0 };
  w.localStorage.setItem("pc_name", "tester");
  w.localStorage.setItem("pc_lang", lang);
  w.localStorage.setItem("pc_base", JSON.stringify(state));
  w.fetch = (url) => new Promise(() => {});          // nothing answers: ops stay queued
  w.WebSocket = function () { this.close = () => {}; };
  w.eval(SCRIPT);
  const doc = w.document;
  const rows = () => [...doc.querySelectorAll("#list li.item")];
  const edit = i => rows()[i].querySelector(".edit").dispatchEvent(new w.Event("click", { bubbles: true }));
  const ops = () => JSON.parse(w.localStorage.getItem("pc_queue") || "[]");
  const nameBox = () => doc.getElementById("sheet-name");
  return { w, doc, rows, edit, ops, nameBox };
}
const idx = (b, text) => b.rows().findIndex(r => r.textContent.includes(text));

(async () => {
  console.log("\n--- 1. English: the sheet shows the English name -----------------");
  {
    const b = boot("en");
    await settle();
    const i = idx(b, "Red bell pepper");
    check("the row itself reads in English", i >= 0, b.rows().map(r => r.textContent));
    b.edit(i);
    await settle();
    check("the name box is the English name, not パプリカ", b.nameBox().value === "Red bell pepper", b.nameBox().value);
    check("the title agrees", b.doc.getElementById("sheet-title").textContent === "Red bell pepper");
    b.doc.getElementById("sheet-qty").value = "3";
    b.doc.getElementById("sheet-save").click();
    await settle();
    const op = b.ops().find(o => o.type === "edit");
    check("changing only the quantity sends no rename", op && op.qty_note === "3" && !("name" in op), op);
  }
  {
    const b = boot("en");
    await settle();
    b.edit(idx(b, "Red bell pepper"));
    await settle();
    b.nameBox().value = "Orange bell pepper";
    b.doc.getElementById("sheet-save").click();
    await settle();
    const op = b.ops().find(o => o.type === "edit");
    check("a real change is sent as the new name", op && op.name === "Orange bell pepper", op);
  }

  console.log("\n--- 2. an item with no English name, and a typed English one ------");
  {
    const b = boot("en");
    await settle();
    b.edit(idx(b, "ごま油"));
    await settle();
    check("no English name: the stored name is all there is", b.nameBox().value === "ごま油", b.nameBox().value);
    b.doc.getElementById("sheet-cancel").click();
    b.edit(idx(b, "White rice"));
    await settle();
    check("an English-typed item opens as shown", b.nameBox().value === "White rice", b.nameBox().value);
    b.doc.getElementById("sheet-qty").value = "2";
    b.doc.getElementById("sheet-save").click();
    await settle();
    const op = b.ops().find(o => o.type === "edit" && o.item_id === "i3");
    check("and saving it untouched sends no rename", op && !("name" in op), op);
  }

  console.log("\n--- 3. Japanese: unchanged ---------------------------------------");
  {
    const b = boot("ja");
    await settle();
    b.edit(idx(b, "パプリカ"));
    await settle();
    check("the stored Japanese name", b.nameBox().value === "パプリカ", b.nameBox().value);
    b.doc.getElementById("sheet-qty").value = "2";
    b.doc.getElementById("sheet-save").click();
    await settle();
    const op = b.ops().find(o => o.type === "edit");
    check("no rename when untouched", op && !("name" in op), op);
  }

  console.log(`\n================ ${passed} passed, ${failed} failed ================`);
  if (failed) process.exit(1);
})();
