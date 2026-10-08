/**
 * ios.test.js — what an iPhone home-screen install needs from the page
 * (the owner's wife's phone; PLAN.md 2026-10-07). Static and jsdom checks only:
 * there is no WebKit or iPhone on the build box, so the real thing is verified
 * by hand on the phone (see README "iPhone").
 *
 * Run: cd app && npm install && npm test
 */
const fs = require("fs");
const path = require("path");
const { JSDOM } = require("jsdom");

const HTML = fs.readFileSync(path.join(__dirname, "..", "index.html"), "utf8");
const SCRIPT = HTML.split("<script>")[1].split("</script>")[0];
const MANIFEST = JSON.parse(fs.readFileSync(path.join(__dirname, "..", "manifest.json"), "utf8"));

let passed = 0, failed = 0;
const check = (name, cond, detail = "") => {
  if (cond) { passed++; console.log(`[PASS] ${name}`); }
  else { failed++; console.log(`[FAIL] ${name}  ${JSON.stringify(detail)}`); }
};
const settle = () => new Promise(r => setTimeout(r, 20));

console.log("\n--- 1. installable on the iPhone home screen ----------------------");
{
  const meta = n => (HTML.match(new RegExp(`<meta name="${n}" content="([^"]*)"`)) || [])[1];
  check("standalone web app", meta("apple-mobile-web-app-capable") === "yes");
  // translucent draws the page under the status bar: clock and every panel's ✕ go unreadable
  check("opaque status bar, so nothing hides under it", meta("apple-mobile-web-app-status-bar-style") === "black",
    meta("apple-mobile-web-app-status-bar-style"));
  check("home-screen name set", meta("apple-mobile-web-app-title") === "ThinCart");
  check("touch icon present", /<link rel="apple-touch-icon" href="\/icon-192\.png">/.test(HTML));
  check("manifest is standalone with icons", MANIFEST.display === "standalone" && MANIFEST.icons.length >= 2);
  check("the quantity box is 16px, so iOS does not zoom on focus",
    /#qty \{[^}]*font-size:1rem/.test(HTML));
  check("no long-press callout on list rows", /li\.item \{[^}]*-webkit-touch-callout:none/.test(HTML));
}

function boot(userAgent, platform = "Linux x86_64", touch = 0) {
  const dom = new JSDOM(HTML, { runScripts: "outside-only", url: "https://s.ts.net/", userAgent });
  const w = dom.window;
  Object.defineProperty(w.navigator, "userAgent", { value: userAgent });
  Object.defineProperty(w.navigator, "platform", { value: platform });
  Object.defineProperty(w.navigator, "maxTouchPoints", { value: touch });
  w.localStorage.setItem("pc_name", "tester");
  w.localStorage.setItem("pc_base", JSON.stringify(
    { revision: 1, items: [], stores: [], picks: {}, suggestions: [], away_pending: 0 }));
  w.fetch = () => Promise.resolve({ ok: true, status: 200, json: async () => ({}), text: async () => "" });
  w.WebSocket = function () { this.close = () => {}; };
  w.navigator.geolocation = { getCurrentPosition: (ok, err) => err({ code: 1 }) };
  w.eval(SCRIPT);
  return w;
}

(async () => {
  console.log("\n--- 2. the location help names the right Settings app --------------");
  const IPHONE = "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 Version/17.0 Mobile/15E148 Safari/604.1";
  const ANDROID = "Mozilla/5.0 (Linux; Android 14; Pixel 7) AppleWebKit/537.36 Chrome/120 Mobile Safari/537.36";
  const IPAD = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 Version/17.0 Safari/605.1.15";
  for (const [label, ua, plat, touch, want, not] of [
    ["iPhone", IPHONE, "iPhone", 5, /Privacy & Security/, /Android/],
    ["iPad (reports as a Mac, with a touch screen)", IPAD, "MacIntel", 5, /Privacy & Security/, /Android/],
    ["Android", ANDROID, "Linux armv8l", 5, /Android/, /Privacy & Security/],
  ]) {
    const w = boot(ua, plat, touch);
    w.document.getElementById("gps-test").click();
    await settle();
    const out = w.document.getElementById("gps-out").textContent;
    check(`${label}: help matches the device`, want.test(out) && !not.test(out), out);
  }
  {
    const w = boot(IPHONE, "iPhone", 5);
    w.document.getElementById("lang").click();     // → Japanese
    await settle();
    w.document.getElementById("gps-test").click();
    await settle();
    const out = w.document.getElementById("gps-out").textContent;
    check("Japanese iPhone help too", /プライバシー/.test(out) && !/Android/.test(out), out);
  }

  console.log(`\n================ ${passed} passed, ${failed} failed ================`);
  if (failed) process.exit(1);
})();
