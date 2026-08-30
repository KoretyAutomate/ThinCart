/**
 * allowed_hosts.test.js — does capacitor.config.json actually allow the hosts
 * this app navigates to?
 *
 * This is the test that was missing when v1.0 shipped broken. The launcher's
 * own logic was covered and green; what was wrong lived in the native
 * navigation policy, which no jsdom test can see. The APK installed, the
 * launcher drew, the probe succeeded — and Capacitor then refused the handoff
 * and said nothing, so the app sat on "Connecting…" forever.
 *
 * The trap is that Capacitor's HostMask is LABEL-COUNTED. It splits both mask
 * and host on ".", and bails immediately unless the counts are equal; "*" is a
 * whole-label wildcard, never a substring or a multi-label one. So:
 *
 *     "*.ts.net"  matches  foo.ts.net           (3 == 3)
 *     "*.ts.net"  does NOT match  spark.tail1.ts.net   (3 != 4)   <-- shipped bug
 *     "100.*"     does NOT match  100.112.171.54       (2 != 4)   <-- shipped bug
 *
 * Tailscale MagicDNS names are always <host>.<tailnet>.ts.net — four labels —
 * so every pattern in the first release blocked exactly what it was written to
 * allow, and only "localhost" ever matched.
 *
 * matchesMask below is a port of Capacitor 6's
 * com.getcapacitor.util.HostMask (node_modules/@capacitor/android/capacitor/
 * src/main/java/com/getcapacitor/util/HostMask.java). If that file changes on
 * a Capacitor upgrade, re-read it against this.
 *
 * Run: npm test
 */
const fs = require("fs");
const path = require("path");

let passed = 0, failed = 0;
const check = (name, cond, detail = "") => {
  if (cond) { passed++; console.log(`[PASS] ${name}`); }
  else { failed++; console.log(`[FAIL] ${name}  ${JSON.stringify(detail)}`); }
};

function matchesPart(mask, s) {
  if (mask === undefined || mask === null) return false;
  if (mask === "*") return true;
  if (s === undefined || s === null) return false;
  return mask.toUpperCase() === s.toUpperCase();
}

function matchesMask(mask, host) {
  const maskParts = mask.split(".").reverse();
  const hostParts = host.split(".").reverse();
  if (maskParts.length > 1 && hostParts.length !== maskParts.length) return false;
  const n = Math.min(maskParts.length, hostParts.length);
  for (let i = 0; i < n; i++) {
    if (!matchesPart(maskParts[i], hostParts[i])) return false;
  }
  return true;
}

const allowed = (masks, host) => masks.some(m => matchesMask(m, host));

console.log("\n--- 1. the port reproduces the shipped bug -------------------------");
// Pinned so a Capacitor upgrade that changes the semantics is caught here
// rather than on the phone.
const SHIPPED_V1 = ["*.ts.net", "100.*", "192.168.*", "10.*", "localhost"];
check("'*.ts.net' misses a 4-label MagicDNS name (the v1.0 failure)",
  matchesMask("*.ts.net", "spark-d28c.tailae3b9b.ts.net") === false);
check("'100.*' misses an IPv4 (the v1.0 failure)",
  matchesMask("100.*", "100.112.171.54") === false);
check("v1.0 masks blocked the real host entirely",
  allowed(SHIPPED_V1, "spark-d28c.tailae3b9b.ts.net") === false);
check("'*.ts.net' does still match a 3-label ts.net name",
  matchesMask("*.ts.net", "foo.ts.net") === true);
check("'*' alone matches anything (single-label mask skips the count check)",
  matchesMask("*", "a.b.c.d.e") === true);

console.log("\n--- 2. the SHIPPING config allows every address we tell people to use ---");
const cfg = JSON.parse(fs.readFileSync(
  path.join(__dirname, "..", "capacitor.config.json"), "utf8"));
const MASKS = cfg.server.allowNavigation;
check("allowNavigation is present", Array.isArray(MASKS) && MASKS.length > 0, MASKS);

// Every shape the README, the setup screen's placeholder, or normalizeServerUrl
// can produce. A host here that is not allowed is an app that hangs on launch.
const MUST_ALLOW = [
  ["spark-d28c.tailae3b9b.ts.net", "this tailnet's real MagicDNS name"],
  ["spark-d28c.example-tailnet.ts.net", "any MagicDNS name (4 labels)"],
  ["foo.ts.net", "a 3-label ts.net name"],
  ["100.112.171.54", "the tailnet IP fallback in the README"],
  ["100.64.0.1", "the bottom of Tailscale's CGNAT range"],
  ["100.127.255.254", "the top of Tailscale's CGNAT range"],
  ["192.168.1.50", "a LAN address"],
  ["spark.local", "an mDNS name"],
  ["localhost", "localhost"],
];
for (const [host, why] of MUST_ALLOW) {
  check(`allows ${host} — ${why}`, allowed(MASKS, host), MASKS);
}

console.log("\n--- 3. and nothing beyond them (Codex P1, 2026-08-30) --------------");
// The first fix used "*.*.*.*" to cover IPv4. That is label-complete, so it
// worked — and it also matched every OTHER four-label host: public IPs, and
// names like a.b.evil.com. allowNavigation decides what the WebView treats as
// app content, so it needs to name the private ranges, not "four of anything".
const MUST_BLOCK = [
  ["8.8.8.8", "a public IPv4"],
  ["1.2.3.4", "any public IPv4"],
  // 100.0.0.0/8 is mostly PUBLIC. Only 100.64.0.0/10 is the CGNAT range
  // Tailscale hands out, so the mask enumerates second octets 64-127 rather
  // than taking all of 100.*.
  ["100.1.2.3", "public 100.x, below Tailscale's CGNAT range"],
  ["100.63.255.255", "the address just below 100.64.0.0/10"],
  ["100.128.0.1", "the address just above 100.64.0.0/10"],
  ["172.16.0.1", "RFC1918 we do not claim to support"],
  ["a.b.evil.com", "a four-label public hostname"],
  ["ts.net.evil.com", "a lookalike that merely contains ts.net"],
  ["evil.com", "an unrelated host"],
];
for (const [host, why] of MUST_BLOCK) {
  check(`blocks ${host} — ${why}`, !allowed(MASKS, host), MASKS);
}
check("'*.*.*.*' would have allowed a public IP (why it was dropped)",
  matchesMask("*.*.*.*", "8.8.8.8") === true);

console.log(`\n================ ${passed} passed, ${failed} failed ================`);
process.exit(failed === 0 ? 0 : 1);
