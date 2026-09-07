# ThinCart — shared shopping list with purchase-cycle & plant-diversity intelligence — PLAN

A self-hosted **PWA shopping list** shared between two phones (user + wife) with
**real-time sync** (the must-keep feature), plus two intelligence layers no
off-the-shelf app offers:

1. **Auto-recommendations from purchase history** — checking an item off the list
   is a purchase event; inter-purchase intervals estimate each item's cycle
   (weekly / bi-weekly / monthly / …) and surface "you're probably due for X".
2. **Recipe + plant-diversity recommendations** — map purchases to distinct
   edible plants, track progress toward **≥30 different plants per week**
   (gut-microbiome guideline), and use the local DGX LLM to suggest recipes and
   new plants that diversify the diet.

> Status: **approved 2026-07-03 — Phases 0, 1, 2 BUILT & live-verified same day**
> (25/25 tests; live two-client WS test; live vLLM enrichment 3 s / ideas 10 s;
> service on :8123 with linger + nightly backup timer). Wife's phone: **iPhone**.
> Implementation deltas from plan: item ids are client-generated UUIDs (offline
> add→checkoff chains work in dead zones); op queue in localStorage, not
> IndexedDB (page-JS-only, tiny); recipes+diversity share one /api/ideas
> endpoint (6 h cache); diversity post-filtered against 30-day eaten set (LLM
> ignored "different" once in live test).
> Blocked on user (Phase 3 gates): enable Tailscale Serve + HTTPS certs (README
> §setup) → A2HS on both phones → two-phone in-store test → 1-week soak.

---

## Locked product decisions (from clarification with user, 2026-07-03)

| Decision | Choice |
|---|---|
| Hosting | **Self-host on DGX + Tailscale-only** (same pattern as OutfitAdvisor). Both phones must be on the tailnet — **wife's phone needs Tailscale installed + invited to the tailnet** (one-time setup, user action). |
| App form | **PWA** served by the backend; both phones "Add to Home Screen". No APK, no store. *(Revised 2026-08-23: Android additionally gets a sideloaded Capacitor shell — see the change-log entry. Still no store, and still the same served PWA inside.)* |
| Purchase history | **Start fresh** — no import. Frequency estimates mature after ~3–4 purchase cycles per item; until then the app is a synced list. |
| Recommendations engine | **Local LLM on DGX** — vLLM Qwen3.5-122B at `:8000` (OpenAI-compatible, `enable_thinking:false` mandatory, else empty output — see OutfitAdvisor empirical finding). Rule-based fallbacks where the LLM is optional. |

---

## Architecture

```
 ┌── Phone A (user, tailnet) ──┐      ┌── Phone B (wife, tailnet) ──┐
 │  PWA in browser / installed │      │  PWA in browser / installed │
 │  - optimistic local UI      │      │  - optimistic local UI      │
 │  - WebSocket live updates   │      │  - WebSocket live updates   │
 │  - offline op queue         │      │  - offline op queue         │
 └──────────────┬──────────────┘      └──────────────┬──────────────┘
                │        Tailscale (private tailnet) │
                ▼                                    ▼
 ┌────────────── DGX spark-d28c (100.112.171.54:8123) ───────────────┐
 │  FastAPI (single service, systemd user unit)                      │
 │   ├─ GET  /            → PWA static files (index.html, sw.js,     │
 │   │                      manifest.json — all vanilla JS, no build) │
 │   ├─ REST /api/*       → list CRUD, history, recommendations      │
 │   ├─ WS   /ws          → broadcast of every list mutation          │
 │   ├─ SQLite (WAL mode) → items, purchase_events, item_catalog     │
 │   └─ LLM client        → vLLM :8000 (canonicalization, plant       │
 │                          mapping, recipes) — always with fallback  │
 └────────────────────────────────────────────────────────────────────┘
```

- **One service, one process, one DB file.** No Redis, no message broker —
  two clients don't need one. WebSocket fan-out is an in-process set of
  connections.
- **Server is the source of truth; WS is downstream-only.** Mutations go over
  REST `POST /api/op` (retryable); the WS only receives broadcasts. Every
  applied op gets a monotonic `revision`; server broadcasts `{op, item, revision}`.
- **Idempotent ops** (store Wi-Fi loses ACKs → clients replay): every op carries
  a client-generated UUID `op_id`; the server keeps an applied-op ledger and
  silently re-ACKs duplicates — a checkoff replayed twice logs **one** purchase
  event. Ops targeting an item id that no longer exists are no-op ACKs, which
  also resolves the checkoff-vs-remove race (whichever lands second is absorbed).
  **Add is idempotent per NFKC-normalized name** — both phones adding "milk"
  offline converges to one row, not two.
- **Reconnect / reconciliation:** flush queued ops in order, then
  `GET /api/state` and fully replace local state. Full-state resync (tens of
  items) avoids all delta-merge complexity; `last_revision` exists only to
  detect missed broadcasts. Reconnect + resync triggers: WS close (backoff
  1→15 s), **`visibilitychange`** (screen unlock — phones kill the WS on lock;
  this is the *normal* in-aisle path, not an edge case), and `online`. UI shows
  a synced/offline pill so a stale list is never silently trusted.
- **In-store dead zones:** service worker caches the app shell; ops queue in
  IndexedDB. Queue flush runs in **page JS** on the events above — not SW
  Background Sync (unsupported on iOS).

### HTTPS is required, not polish
Service workers and installable PWAs need a **secure context** — plain
`http://100.112.171.54:8123` gets no offline caching and no reliable A2HS.
Fix (Phase 0): enable tailnet HTTPS once and front the service with
`tailscale serve` (Let's Encrypt cert on the MagicDNS name); phones open
`https://spark-d28c.<tailnet>.ts.net`. The app still binds
`100.112.171.54:8123` — tailscale serve only proxies.

### Identity (minimal)
No accounts. Each client picks a display name once ("Korehito" / wife's name),
stored in `localStorage`, sent with every op — so the UI can show "✓ milk
(bought by …)". If iOS ever evicts localStorage, the app just re-prompts.
Tailnet membership *is* the auth boundary (same trust model as OutfitAdvisor
MVP). Server binds the tailnet IP only.

---

## Data model (SQLite, WAL)

```sql
items(              -- the live list
  id INTEGER PK, catalog_id INT NOT NULL, qty_note TEXT,      -- "2 packs"
  added_by TEXT, added_at TEXT, revision INT)

purchase_events(    -- history: the intelligence substrate (undo may delete)
  id INTEGER PK, catalog_id INT NOT NULL,
  bought_at TEXT NOT NULL, bought_by TEXT,
  source TEXT CHECK(source IN ('checkoff')))

item_catalog(       -- one row per *canonical* item ever seen
  id INTEGER PK, canonical_name TEXT UNIQUE,   -- "たまねぎ" ≡ "玉ねぎ" ≡ "onion"
  display_name TEXT, aliases_json TEXT,
  category TEXT,                                -- produce / dairy / pantry …
  plants_json TEXT,      -- DISTINCT edible plants this item contributes,
                         -- e.g. curry roux → ["wheat","turmeric","cumin",…]; milk → []
  is_edible INT, snoozed_until TEXT,            -- snooze is server-side: syncs
  llm_enriched_at TEXT)                         -- to BOTH phones

meta(key TEXT PK, value TEXT)                   -- global revision counter
applied_ops(op_id TEXT PK, applied_at TEXT)     -- idempotency ledger, pruned >7 d
```

**Three gestures (critical for history quality):**
- **Check off (default tap)** → removes from list **and** logs a
  `purchase_event`. This is the shopping gesture.
- **Remove without buying (long-press → "remove")** → removes from list,
  **no** event. Keeps the frequency data clean (changed your mind ≠ bought).
- **Undo (toast, ~10 s after checkoff)** → an op that re-adds the item and
  **deletes its purchase_event** — a fat-finger must not poison the intervals.

**List ordering:** group by `category`, then `added_at` — coarse aisle grouping
for free from the enrichment data. No manual reorder in MVP.

**Canonicalization** (Japanese + English, full-width/half-width, spelling
variants): on first add of an unseen name, the server asks the LLM to match it
against existing catalog entries or create a new one (with plant mapping +
category in the same call, cached forever in `item_catalog`). Fallback when LLM
is down: exact-normalized-string match (NFKC fold), enrich lazily later via a
nightly sweep. Per workspace regex rule: all string normalization handles
full-width Japanese characters.

---

## Intelligence layer 1 — purchase-cycle recommendations (pure rules, no LLM)

For each catalog item with **≥3 purchase events**:
- intervals = successive `bought_at` deltas (days), after **coalescing events
  <1 day apart into one** (burst buys and double-checkoffs must not crush the
  median toward zero); estimate = **median**. The median absorbs *one* stray
  trip, but not a household that travels regularly — hence layer 1b, which
  measures the deltas in **in-town days** instead of calendar days.
  Classify into bins: ≤4.5 d → "twice a week", ≤9 d → "weekly",
  ≤18 d → "bi-weekly", ≤45 d → "monthly", else "occasional".
- **Due score** = days_since_last / median_interval. Suggest when
  **0.85 ≤ score ≤ 3.0** and the item isn't already on the list; sort by score.
  The upper cap retires lapsed/seasonal items (strawberries in August) instead
  of nagging forever.
- UI: a "Suggested" tray above the list — one tap adds, swipe dismisses.
  Dismissal sets `snoozed_until` = now + ½ interval **on the server**, so one
  spouse's dismissal silences both phones; no event logged.
- Items with <3 events simply never appear — no cold-start noise.

Deterministic, testable with synthetic histories, zero LLM dependency.

## Intelligence layer 1b — cycles measured in in-town days (Google Calendar)

A week away from home is not a week of groceries. Calendar-day intervals count
travel as consumption, so every cycle an item has is inflated by however much
the household happened to be gone, and the suggestion arrives late. Layer 1b
subtracts the days spent out of town from every interval the estimator sees.

**Unit change.** `median_days` and `days_since` are now *in-town* days. A
"weekly" item is one bought every 7 days **at home**; the label bins are
unchanged, because the quantity they describe is the one the household
actually consumes against. With no away days recorded the arithmetic is
identical to layer 1 — this is a refinement, not a replacement.

**Subtraction is fractional, not whole-day.** Each away day contributes the
overlap between its home-local midnight-to-midnight window and the interval,
so a trip that starts mid-afternoon costs a fraction of that day, not all of
it. Whole-day rounding on a 3-day gap is a >30 % error.

- **Coalescing stays on wall-clock time.** Two checkoffs 20 minutes apart are
  one shopping trip whether the household is home or in Boston; that rule is
  about the physical act of shopping, not about consumption.
- **Floor at zero, and never divide by it.** A pathological history (bought,
  left town, returned, bought) can make an interval all-travel; intervals
  clamp at 0 and a median of 0 disables suggestions for that item rather than
  producing an infinite due score.

### Where away days come from

Read-only Google Calendar over OAuth (`calendar.readonly`). Credentials live
in `~/.config/thincart/google_oauth.json`, **outside the repo** — ThinCart is
a public repo and this file is a bearer credential. `calendar_sync.py
--authorize` runs the consent flow once and stores the refresh token; the
server refreshes access tokens itself and polls every 6 h. Calendar failure is
never fatal: a sync error logs and leaves the previous away set in place.

### Detection is a proposal, not a verdict

The calendar does not have a "travel" field, so detection is heuristic:
`OUT_OF_OFFICE` events, all-day events spanning ≥2 days, and hotel/flight/trip
wording (including Gmail's auto-created `Stay at …` / `Flight to …` bookings,
which is how the real Jul 31–Aug 2 Boston trip appears). Timed single events
are never travel — an evening dinner reservation or a Saturday open house is a
day at home.

Every detected day lands in `away_days` with status `auto` and is **shown for
review** in the Travel panel. The user confirms or rejects; `confirmed` and
`rejected` are decisions and a later sync must never overwrite them. Days can
also be marked away by hand — a manual entry is born `confirmed`, because
typing it in *is* the decision.

**Only `confirmed` days affect the cycle arithmetic** (`db.away_set`). An
`auto` row is inert: visible, one tap from counting, and until then changing
nothing. This is what makes the review real rather than cosmetic — the first
sync ingests 180 days of calendar in one pass, so admitting proposals would
let a single bad match (the genuine 12-day hotel booking in the household's
own town) silently reshape every cycle, suggestion and snooze deadline in the
app before anyone had looked at it. That is the fully-automatic behaviour this
design was chosen over.

The cost is that the feature does nothing until someone reviews the first
batch, which the Travel panel's badge and its "not counted yet" heading say
plainly.

## Intelligence layer 1c — whole-week cycles over the entire purchase history

Three changes, all pulling the same way: describe the household's rhythm in the
unit it actually shops in, over everything it has ever bought, and never let a
weak estimate pass for a strong one.

### Scope is every item ever bought

`/api/cycles` returns all of them, not just the ones with a learned median.
Restricting the panel to ≥3-purchase items made the app look like it had
forgotten a purchase it had in fact recorded — on a 28-day history with 6
shopping trips, that was 14 items out of 89. What separates a thin item from a
settled one is how much is *claimed* about it, not whether it is listed.

### Cycles are grouped in whole weeks, open-ended

weekly, bi-weekly, every 3 weeks, every 4 weeks, … with no top bin. Shopping
runs on a weekly rhythm, so "every 3 weeks" is a sentence about this household;
"monthly" was a bin that silently merged 3-week and 6-week items, and
"occasional" said nothing at all.

**The week is for grouping and display only.** Due-scoring always uses the
measured interval in days, so a 17-day item is labelled bi-weekly and judged at
17 days — never at the 14 its label rounds to.

### Two tiers, and they answer a shopping question

- **HIGH — buy now.** Due (≥0.85× its cycle) on a rhythm worth trusting: ≥3
  purchases whose recent gaps agree (spread ≤ 1.0, i.e. the widest at most
  about double the narrowest).
- **POTENTIAL — might need this week.** Either due now but the evidence is thin
  or erratic, or not due yet and arriving within the coming week.
- Everything else is listed with its rhythm and no call to action.

Tiering on estimator confidence alone was the wrong axis. "This estimate has a
wide spread" is not something anyone can act on in a supermarket aisle.
Evidence quality still decides *which* tier a due item lands in, but the tier
itself is about what goes in the basket. A shaky cycle also retires at 2×
rather than 3×, so a guess stops nagging sooner than a known rhythm does.

Consistency remains a separate axis from purchase count on purpose: bought at
7, 8, 7 days is a different claim from 4, 25, 9, and counting purchases cannot
tell them apart. On the real history this is what separates オレンジジュース
(spread 1.07) and バナナ (1.43) from the onions (0.07).

### "This week" is in-town days, with a half-cycle floor

The horizon is the in-town days the next seven calendar days actually contain,
so a week that is mostly a trip pulls almost nothing forward — and a week spent
entirely away pulls nothing at all, which is correct and is only knowable
because of the calendar link.

The horizon alone cannot decide anything for an item whose cycle is already
shorter than a week: milk bought yesterday is "due within 7 days" and would sit
in POTENTIAL permanently — for the 13 weekly items in this household, that is
most of the list, most of the time. So an item must ALSO be at least halfway
through its cycle before it can be called coming-up. Before that, it
demonstrably still has some.

### Only the recent rhythm counts

The estimate uses the last **4** intervals. A household's rhythm drifts, and a
gap from four months ago is evidence about a routine that may no longer exist.

## Intelligence layer 2 — plants & recipes (LLM with graceful fallback)

- **Plant counter (rule-based, always on):** distinct plants = union of
  `plants_json` over purchase events in the trailing 7 days. Header widget:
  **"🌱 23 / 30 plants this week"** with the list on tap. The mapping comes
  from the cached LLM enrichment, so the *counter* itself works even when the
  DGX LLM is down. Enrichment must return plants as **canonical lowercase
  English tokens** ("wheat", never 小麦/Wheat/komugi) or the cross-language
  union double-counts. Known undercount: monthly-bought staples (rice, flour)
  fall out of a 7-day purchase window while still being eaten — accepted for
  MVP; if the count feels low, widen to 30 days for `pantry`-category items.
- **Canonical vocabulary + weighted points (2026-07-12):** "canonical lowercase
  English token" was too weak a spec — the LLM emitted `capsicum` for one bell
  pepper and `pepper` for another (double-count), one `pepper` for both capsicum
  and the black-pepper spice (collision), and `citrus` for lemon *and* lime
  (collision) while splitting `orange` (inconsistent granularity). The token unit
  is now the **culinary taxon**: one token per species, except where a species is
  eaten in two unrelated roles (`bell pepper` vs `chili pepper`, both *Capsicum
  annuum*). Colour, cultivar, brand and refinement never split a token
  (green/red/yellow bell pepper → `bell pepper`; white/brown/purple rice →
  `rice`). `server/plants.py` holds the alias map + context-resolved ambiguous
  tokens + weights, and is the safety net the flaky local LLM cannot drift past —
  it normalizes on **write and on read**, so the count is right with the DGX down.
- **Counting method = AGP, not Rossi (user decision 2026-07-12).** Three systems
  exist and they disagree: the **American Gut Project** (McDonald et al. 2018 — the
  study that produced the number 30) is a *plain count*, no fractions, no
  exclusions (its own survey: a soup of carrot+potato+onion = 3 plants; every grain
  in multigrain bread counts; herbs, spices and juices each score a full 1).
  **ZOE/Spector** publish no fractions either. Only **Megan Rossi's "plant points"**
  has fractions (herbs/spices/garlic/olive oil/tea/coffee = ¼). Rossi keeps the
  target at 30 while making 30 strictly harder to reach, so its 30 ≠ the study's 30.
  We take AGP so the **target and the method come from the same source**.
  `plants.COUNTING_MODE = "agp"` is the single chokepoint; Rossi's weight table is
  retained and switchable (`= "rossi"`) — both modes are tested.
  Known trade-off: a flat count is gameable (one processed food with a long
  ingredient list can donate ~7 points) — accepted, because that is exactly what
  the study measured.
  - `Delegation: sub-agent (research + vocabulary + counter); director reviewed,
    re-ran the suite independently, and reversed the weighting to AGP per user.`
- **Diversity suggestions (LLM):** "Plants you haven't bought in 30+ days +
  plants that pair with what's already on your list" → tap to add to list.
- **Recipes (LLM):** on demand ("What can we cook?"), from the last ~10 days of
  purchases: ~3 recipes using what you have, each annotated with **+N new
  plants** if you add 1–2 ingredients. Recipe screen has "add missing
  ingredients to list" per recipe.
- vLLM call contract (from OutfitAdvisor, empirically verified):
  `chat_template_kwargs: {"enable_thinking": false}`, bounded `max_tokens`,
  JSON-schema-constrained responses; timeout → hide the feature, never block
  the list. **The list + sync must work with the LLM completely offline.**

---

## Build sequence (each step verified by execution, not review)

**Phase 0 — skeleton + realtime sync (the must-keep feature, de-risk FIRST)**
1. FastAPI + SQLite schema + `/api/state` + `POST /api/op`
   (add/checkoff/remove/undo, `op_id` dedupe) + WS broadcast; systemd user
   unit binding `100.112.171.54:8123` (kill-before-restart per workspace
   rule) + `loginctl enable-linger` (must survive a DGX reboot with no SSH
   login); enable tailnet HTTPS + `tailscale serve` in front (see §HTTPS).
2. Minimal PWA: list UI, add box, tap-to-checkoff, long-press remove, undo
   toast, WS client with auto-reconnect + resync on `visibilitychange`/
   `online`, sync-status pill, display-name prompt.
3. **Verify with two real phones in the store parking lot** (not just two
   browser tabs): mutation on phone A visible on phone B < 2 s; airplane-mode
   phone A, add 2 items + check one off, re-enable → B converges, exactly one
   purchase_event; **replay the same op twice → still one event**; lock both
   phones 2 min, mutate, unlock → both converge without manual refresh.
   Two-tab test first; the two-phone test is the acceptance gate.

**Phase 1 — history + cycle recommendations**
4. `purchase_events` logging on checkoff; canonicalization (NFKC fallback path
   first, LLM path second); unit tests for the interval estimator with
   synthetic histories (weekly item, biweekly with one skip, new item).
5. Suggested-tray UI + snooze. Verify: seed synthetic history via a fixture
   script, confirm correct items surface with correct cycle labels.

**Phase 2 — plants + recipes**
6. LLM enrichment call (catalog caching, nightly sweep for missed items);
   plant counter widget; verify counts against a hand-checked week of data.
7. Recipe + diversity endpoints and screens; verify JSON-schema outputs, the
   +N-new-plants annotation, and the LLM-down fallback (feature hidden, list
   unaffected).

**Phase 3 — polish + install**
8. PWA manifest/icons/service-worker shell caching; Add-to-Home-Screen on both
   phones; wife's-phone Tailscale onboarding (user action, documented in
   README); nightly `sqlite3 .backup` cron to `~/backups/shopping-list/`
   (keep 14 — the history DB *is* the intelligence; losing it resets the app);
   1-week real-usage soak.

Per workspace rules: test outputs saved to `test_results/<name>_<date>.txt`;
syntax-validate multi-file edits; commit after each phase gate.

**Out of MVP (banked):** multiple named lists, manual reorder / per-store aisle
order, price tracking, quantity math beyond the free-text `qty_note`, accounts,
public HTTPS host.

---

## Post-MVP change log

### 2026-07-12 — Specificity fixes (brand/type preservation) + long-press editor
Delegation: considered, rejected — debugging + subtle cross-file UI/backend changes
(catalog.py + db.py + app.py + index.html) needing design judgment on
canonicalization aggressiveness and gesture integration; not mechanical/voluminous,
no machine-checkable spec short of the output itself (delegation.md "do NOT delegate").

User bug report (live shopping trip 2026-07-12): (1) "One Mighty Mill bagel" → bagel
(brand lost); (2) "White Rice" → rice, "Fettuccine"/"spaghetti" collapsed into pasta;
(3) "Yellow squash" → zucchini; (4) plant count included un-bought items; (5) want
long-press → adjustment screen for category/quantity. User choices: preserve
brands+types (still merge true synonyms); un-merge existing collapsed rows.

Root cause 1-3 (confirmed in live DB): the `alias_of` LLM merge (+ a seeded
spaghetti→パスタ alias) folded specific/branded items into the generic seed rows, and
`name_en` then showed the generic English alias instead of the typed text.

**Fixes shipped:**
- `db.name_en`: an ASCII (English-typed) display now ALWAYS wins over any banked
  generic alias — "White rice"/"One Mighty Mill bagel" show as typed; the alias
  fallback is reserved for Japanese displays.
- `catalog.enrich`: (a) only banks the LLM `english_name` as an alias for non-ASCII
  (JP) displays — never shadows an English name; (b) new deterministic `_is_variety`
  backstop blocks any alias merge where the new item is a qualifier-superset of the
  target ("white rice"⊃"rice", "fettuccine pasta"⊃"pasta"); (c) prompt rewritten to
  keep brands/types/varieties distinct with the exact failing examples.
- `apply_edit` op (+ Op.category field): long-press editor writes `items.qty_note`
  and `item_catalog.category` (validated against CATEGORIES); idempotent, noop on
  vanished item. Optimistic in `view()`.
- Frontend: long-press sheet is now a full editor (quantity input + 9-category
  picker + Save, keeping skip/remove); hold bumped 500→600 ms; EN/JA strings; sw v3.
- **Issue 4 (no backend bug):** reconciled the op ledger — 43 checkoff ops, 4 undone
  → 39 `purchase_events` → 29 plants, ALL from client checkoffs. The count only ever
  reflects checked-off items; no phantom-count path exists. Likeliest cause is a
  reflow mistap (checkoff removes a row, the list jumps, a follow-up tap lands on the
  shifted row). Added a 350 ms post-removal tap/swipe lockout to prevent it.
- **Data repair (un-merge):** backup → `~/backups/shopping-list/thincart-preunmerge-2026-07-12.db`;
  split "yellow squash", "white rice", "spaghetti", "fettuccine", "One Mighty Mill
  bagel" back into their own catalog rows; stripped the bad aliases off ズッキーニ/米/
  パスタ/ベーグル (kept the legit translation aliases). Past `purchase_events` stay on
  the generic rows (user's choice) so today's plant count is unchanged. `seed_catalog`
  パスタ aliases trimmed to `["pasta"]` so a fresh seed won't recollapse.
- Tests: +9 in `tests/test_specificity.py` (name_en preservation, `_is_variety`,
  enrich merge-block vs true-alias-merge, edit op qty/category/validation/noop).
  Full suite **47 passed** (test_results/specificity_fixes_2026-07-12.txt). Live-verified
  on :8123 after restart: catalog rows distinct, edit round-trip persists qty+category.


### 2026-07-11 — Purchase-history panel (mis-swipe repair) + history reset
Delegation: considered, rejected — subtle cross-file UI feature (app.py + db.py +
index.html) needing design judgment on panel/gesture integration, not mechanical
or voluminous; no machine-checkable spec short of the output itself.

- **Reset:** cleared test purchase data before real use — wiped `purchase_events`
  (29) + `applied_ops` (44) + expired `snoozed_until` (2); kept the 176-row
  `item_catalog` (typing corpus) and the monotonic `revision`. Safety copy taken
  first via `sqlite3 .backup`.
- **Feature (why):** a mis-swipe (→ checkoff by accident) logs a spurious
  `purchase_event` that pollutes the cycle estimator, and the ~8 s undo toast
  can't reach it once dismissed. Mistypes were already covered (swipe-left = skip,
  no event). Gap = correcting a purchase *after the fact*.
- **Backend:** new `undo_purchase` op keyed by the server `purchase_events.id`
  (works for ANY past purchase, unlike `undo_checkoff` which is bounded by the
  7-day op ledger) — deletes the event, re-adds the item to the list, deduped;
  unknown/already-deleted event → no-op ACK; idempotent via the op ledger.
  New `GET /api/history` (newest-first, joined to catalog). `db.recent_history()`.
- **Frontend:** header 🕘 button → full-screen History panel (mirrors the cycles
  panel); each row shows item / when / who + a "Not bought" button that fires
  `undo_purchase` and toasts. EN/JA strings added. `sw.js` cache → v2.
- Tests: +4 in `test_ops.py` (history listing, undo repair, unknown-event no-op,
  replay idempotency).

---

## Top risks

1. **Wife-phone adoption friction** — Tailscale install + PWA on her phone is
   the whole product for her. Mitigate: her flow is identical to today's app
   (open, add, tap off); all intelligence lives on the user's screens too.
2. **Phone-lock kills the WebSocket** — on both platforms, and it happens
   dozens of times per shopping trip (pocket the phone, walk an aisle, unlock).
   The design treats unlock-resync as the primary path, not an edge case; the
   Phase 0 lock/unlock test is the gate. **Confirm the wife's phone OS before
   Phase 3 icon/manifest polish** (iOS additionally restricts A2HS to Safari).
3. **Checkoff ≠ purchase noise** (deleting things you didn't buy) — mitigated
   by the three-gesture design (checkoff / remove / undo); the split must be
   obvious in the UI.
4. **LLM canonicalization latency on add** — adding an item must feel instant:
   the add is optimistic + NFKC match; LLM canonicalization runs async and
   merges catalog entries after the fact.
5. **:8000 vLLM contention** with podcast/screener jobs — calls are rare
   (new-item enrichment, on-demand recipes) and bounded; timeouts degrade
   gracefully.

---

## Repo / file layout

```
shopping-list/
├─ server/
│  ├─ app.py            # FastAPI: static, REST, WS, revision counter
│  ├─ db.py             # SQLite schema + migrations
│  ├─ catalog.py        # canonicalization (NFKC + LLM), plant enrichment
│  ├─ cycles.py         # interval estimator + due scoring (pure functions)
│  ├─ llm.py            # vLLM client (enable_thinking:false, JSON schema)
│  ├─ requirements.txt  # fastapi, uvicorn, httpx
│  └─ deploy/           # systemd user unit (binds 100.112.171.54:8123),
│                       # tailscale-serve setup + nightly-backup cron notes
├─ app/
│  ├─ index.html        # single-file PWA UI (vanilla JS; op queue + flush
│  │                    #   live here — page JS, not the SW)
│  ├─ sw.js             # app-shell cache only
│  └─ manifest.json
├─ mobile/              # Capacitor 6 Android shell (2026-08-23)
│  ├─ www/index.html   # launcher ONLY: asks/remembers the server address,
│  │                    #   probes it, hands the WebView to the real PWA
│  ├─ android/         # native project (no custom code; icons + signing only)
│  └─ tests/           # jsdom tests for the launcher's branch logic
├─ tests/               # estimator, canonicalization, sync-op unit tests
├─ test_results/
├─ .github/workflows/build-apk.yml   # CI APK build (the DGX is aarch64)
└─ PLAN.md
```

---

## Phase 4 — per-item emoji icons (2026-07-15)

Item rows show an icon that looks like the actual item (🥑 avocado, 🍌 banana)
instead of only the category emoji. Three-tier resolution: curated map
(`server/emoji.py`, ~120 EN+JA items, instant/offline) → LLM-picked emoji at
enrich time for anything unmatched (validated single-grapheme) → category emoji
as the UI floor. New `item_catalog.emoji` column (migration in `db.connect`),
carried in `state()`; frontend `render()` prefers `it.emoji`. Backfill:
`scripts/backfill_emoji.py` (74/208 live rows on first run). Tests: `test_emoji.py`.

Delegation: considered, rejected — emoji-map authoring needs per-item judgment
and Unicode/rendering correctness (not cleanly machine-checkable), and the wiring
is subtle cross-file changes on the live service (db + catalog + frontend).

Brand images (product logos for branded items): DEFERRED. Legally fine for the
private tailnet app (nominative fair use, no distribution) but NOT for any public
build (trademark/copyright in commerce) — never bake brand assets into the SaaS
build. Emoji is the legally-clean default everywhere.

---

## Phase 5 — stores, notes & purchase criteria + where-to-buy plan (2026-07-18)

User request: (1) notes / store names on items, (2) purchase criteria (quantity,
budget, …), (3) recommendations of WHERE to buy each item, driven by the stored
store information.

Delegation: considered, rejected — extends the live sync protocol (Op model,
state shape, offline queue semantics) and needs UI/UX judgment throughout;
the plan would be as long as the diff and nothing is cleanly machine-checkable
in isolation.

### Data model (additive migrations only — live DB stays valid)

```sql
CREATE TABLE stores(
  id INTEGER PRIMARY KEY,
  name TEXT NOT NULL,                  -- display, as first typed
  canonical_name TEXT UNIQUE NOT NULL, -- db.canonical() of name
  notes TEXT NOT NULL DEFAULT ''       -- "cheap produce, good fish" — feeds LLM placement
);
ALTER TABLE item_catalog ADD COLUMN note TEXT NOT NULL DEFAULT '';   -- criteria: brand/size/budget text
ALTER TABLE item_catalog ADD COLUMN budget REAL;                     -- typical price ¥/one purchase
ALTER TABLE item_catalog ADD COLUMN preferred_store_id INTEGER REFERENCES stores(id);
ALTER TABLE purchase_events ADD COLUMN store_id INTEGER REFERENCES stores(id);
```

Note/budget/preferred-store live on `item_catalog` (not `items`) deliberately:
criteria persist across checkoff→re-add cycles — that is what makes them
"purchase criteria" rather than one-trip remarks. Per-trip text stays `qty_note`.

### Ops (Op model extensions, all idempotent like existing ops)

- `edit` gains `note`, `budget`, `store` (store display name; `""` clears the
  preferred store; unknown name auto-creates the store row).
- `checkoff` gains optional `store` — stamps `purchase_events.store_id`
  (auto-create by name). Fed by the client's "I'm at: <store>" selector.
- new `store_upsert` (`store_name`, `store_notes`) — create store / update notes.
- new `store_delete` (`store_id`) — nulls `preferred_store_id` and
  `purchase_events.store_id` references, deletes the row (typo repair).

### Recommendation rule (pure rules; LLM only as opt-in gap-filler)

Per catalog item: explicit `preferred_store_id` → else most-frequent
`store_id` over its purchase history (tie: most recent) → else unassigned.
Source tag ("preferred"/"history") is carried so the UI can show why.

- `state()` items gain `note`, `budget`, `store`, `store_source`.
- new GET `/api/stores` — store list with notes (client cache for pickers).
- new GET `/api/plan` — current list grouped by recommended store, with a
  per-store budget subtotal (sum of known budgets) + unassigned bucket.
  `?llm=1` additionally asks the DGX LLM to place unassigned items using the
  store notes; LLM failure degrades to rule-only (never blocks, like /api/ideas).

### PWA UI

- Edit sheet (long-press): note field, budget field (numeric), store picker —
  chips of known stores + free-text for a new one.
- Item row: note shown muted next to qty; store chip (e.g. 「OKストア」).
- New Stores panel (header 🏬): shopping plan grouped by store w/ subtotals
  (the "where to buy" answer); "I'm at:" current-store selector (localStorage,
  stamps subsequent checkoffs — builds history with zero extra effort); store
  list with editable notes + delete.
- `view()` applies the new edit fields optimistically; full EN/JA i18n.

### Gates

- Unit tests (~10 new): store upsert/delete, edit note/budget/store, checkoff
  store stamping, precedence preferred>history, /api/plan grouping + subtotals,
  migration idempotency on an existing DB.
- Live verification: restart thincart.service, /health OK, exercise /api/plan
  and a store-stamped checkoff against 100.112.171.54:8123.

### Review deltas (agent review, 2026-07-18 — applied)

1. Store auto-create is **get-or-create by canonical_name** (plain INSERT on the
   UNIQUE column would 500 on spelling collisions and wedge the client op queue).
2. `catalog.enrich` alias-merge **carries note/budget/preferred_store_id** onto
   the merge target (target wins where already set) — otherwise async merges
   silently discard criteria.
3. `edit` also carries `catalog_id`: catalog-level fields (note/budget/store)
   apply even when the item row vanished mid-edit (spouse checked it off);
   only `qty_note` requires the live item. `state()` items expose `catalog_id`.
4. `budget` travels as a **string**: `""` clears, else lenient parse (NFKC fold
   full-width digits, strip ¥/円/commas); unparseable → field ignored, rest of
   the edit still applies.
5. `stores` is embedded in `state()` (no `/api/stores`, no client cache-staleness).
6. **No `/api/plan` endpoint** — the Stores panel groups `view()` client-side
   (works offline, reflects pending ops). LLM placement CUT from v1; the
   "I'm at:" checkoff stamping earns ground truth within a couple of trips.
7. `stores` uses `AUTOINCREMENT` (rowid reuse + cross-phone offline delete could
   hit the wrong store). Deleting a store nulls references; a lagging offline op
   naming it re-creates it (documented property, acceptable).
8. "I'm at:" selection expires after 6 h (stale selection would silently poison
   where-bought history).

### 2026-08-23 — Android app (sideloaded Capacitor shell)

User request: "build a ThinCart app like the outfit advisor I installed to my
phone" — i.e. a real installed app, off the Play Store. Same shape as
OutfitAdvisor: debug APK from CI, `adb install`.

Delegation: considered, rejected — one small new component, no volume to grind.

**The choice that mattered: shell vs. bundle.** OutfitAdvisor's APK bundles its
whole web layer, because that app's phone side owns the schedule and the GPS and
must work with the server unreachable. ThinCart's does not. Its web layer is
~80 KB of vanilla JS that talks to its own origin — relative `/api/*`, a `/ws`
WebSocket keyed on `location.host`, a service worker scoped to `/`, and an op
queue in that origin's localStorage. Bundling it would mean threading a
configurable base URL through every one of those, adding CORS to the server, and
rebuilding + reinstalling the APK on both phones for every UI change — for an
app whose UI changes most weeks. So:

- **The APK is a shell.** `mobile/www/index.html` is a launcher, not the app: it
  asks for the server address, probes it, and hands the WebView to the live PWA
  at its own https origin. Everything downstream — sync, offline queue, SW —
  is byte-identical to the browser, because it *is* the browser.
- **UI changes ship by restarting the service, not by rebuilding the APK.** CI
  triggers on `mobile/**` only. This is the property the shell was chosen for.
- **The address is asked for at first launch, not compiled in.** This repo is
  public; a tailnet name is not something to publish. Remembered in
  localStorage thereafter.
- **Back is the escape hatch.** `location.assign` (not `replace`) plus a
  sessionStorage flag: a cold start with a saved address goes straight through,
  but Back from the list lands on the settings screen instead of re-launching.
  Without that, an address that is wrong but *reachable* could only be fixed by
  clearing app data.
- **Signed with the same persistent debug keystore as OutfitAdvisor**
  (`~/.android-ci/debug.keystore`, pinned by SHA-256 in the workflow). A key
  change forces a data-wiping uninstall — here that costs the saved address and
  any op queued in a shop dead zone, so the workflow fails rather than ship it.

Build is CI-only: Google ships no aarch64 `aapt2`, so the DGX cannot produce an
APK. `npx cap add/sync` run fine there; only `assembleDebug` needs x86-64.

Verified: 36/36 launcher tests (`test_results/mobile_launcher_2026-08-23.txt`)
covering URL normalization, cold start, unreachable server, Back-return, and
first-run save. APK build + on-device install are the remaining gates.

iPhone is unchanged (A2HS via Safari) — Capacitor could target it, but that
needs a Mac to build and an Apple developer account to sideload, which is a
different project.

**Banked:** in-app updater (OutfitAdvisor's `server/publish_apk.py` + a
versionCode check) — worth it only once the shell changes often enough that
`adb install` is a chore. Today it is one file that rarely moves.

### 2026-08-30 — v1.0 could never leave the launcher (allowNavigation)

Installed on the Pixel, v1.0 sat on "Connecting to spark-d28c…" forever. The
address was right, the server was up, the probe succeeded — and then nothing.

**Cause: Capacitor's `server.allowNavigation` masks are LABEL-COUNTED.**
`HostMask.Simple.matches` (Capacitor 6, `com.getcapacitor.util.HostMask`) splits
mask and host on `.`, and returns false immediately unless the two have the same
number of parts. `*` is a whole-label wildcard — never a substring, never
multi-label. So of the five masks v1.0 shipped:

| mask | intended | actually matches |
|---|---|---|
| `*.ts.net` | any tailnet name | only 3-label `foo.ts.net` — **not** `spark-d28c.tailae3b9b.ts.net` (4) |
| `100.*` | the tailnet IP | nothing (2 parts vs an IPv4's 4) |
| `192.168.*`, `10.*` | LAN | nothing, same reason |
| `localhost` | localhost | localhost — the only one that ever matched |

Tailscale MagicDNS is always `<host>.<tailnet>.ts.net`, four labels. So every
pattern blocked exactly what it was written to allow. Capacitor then declines
the navigation **silently** — no callback, no error page, no log the phone
shows — leaving the launcher on screen with its spinner running.

Two fixes, because the second is what made the first take a week to find:

1. **Masks corrected** to ts.net at both label depths, `*.local`, RFC1918, and
   Tailscale's CGNAT range — and pinned by a test. Getting the IP half right
   took two rounds of the pre-push gate. `*.*.*.*` (P1) is label-complete and
   so it worked, but it matched every other four-label host, public IPs and
   `a.b.evil.com` included. `100.*.*.*` (P2) still trusted the publicly
   routable majority of `100.0.0.0/8`; Tailscale only uses `100.64.0.0/10`.
   `HostMask` has no numeric ranges, so the config now **enumerates second
   octets 64–127** — 64 entries, verbose but exact. `allowNavigation` is what
   decides which origins the WebView treats as app content, and that boundary
   is worth spelling out. Both rejected masks are negative cases in the test,
   as are the addresses just outside the range. `mobile/tests/allowed_hosts.test.js`
   ports HostMask into JS, reads the REAL `capacitor.config.json`, and asserts
   every address shape the README or the setup screen can produce is allowed —
   including the v1.0 masks as negative cases, so a Capacitor upgrade that
   changes the semantics fails here rather than on a phone.
2. **A handoff that never completes is now visible.** `launch()` arms a 6 s
   timer before navigating; if the launcher is still on screen after it, the
   settings screen says so and offers Retry / change-address. It is cancelled
   on `pagehide`, because a successful navigation can park this document in the
   WebView's back-forward cache where timers are paused rather than dropped,
   and Back within the grace period would otherwise have blamed the app for
   something it did correctly.

   It deliberately **names no cause**, and that took three rounds of the gate to
   arrive at. A refusal (Capacitor fires `ACTION_VIEW`, so the address opens in
   the system browser) and a navigation that was accepted but is slow to commit
   are indistinguishable from inside the page. Backgrounding looked like the
   signal that separated them — until you notice that locking the phone or
   following a notification backgrounds the app too. Detecting a refusal
   properly means native code observing the navigation decision, which is a
   real cost for a diagnostic; the honest alternative was to stop asserting.
   The message now states only what is observable — the server answered, the
   app is still here — and points at the address. An unreachable server was already handled;
   a *reachable* server the app declines to open was the gap. The timer is
   cancelled on `pagehide` — Codex caught (P2) that a successful navigation can
   park this document in the WebView's back-forward cache, where timers are
   paused rather than dropped, so Back within the grace period would have
   resumed it and reported a refusal that never happened.

**The lesson, and it is not thincart-specific:** the jsdom suite was green and
stayed green — it covers the launcher's JS, and the bug was in the native
navigation policy, which no jsdom test can reach. A shell app has a seam
between the web layer and the container, and tests that live entirely on one
side of it prove nothing about the other. `allowed_hosts.test.js` exists to sit
*on* that seam. OutfitAdvisor bundles its web layer and never navigates
cross-origin, so it is unaffected — but any future Capacitor app that does
navigate needs this same check.

Verified: 55/55 (`test_results/mobile_launcher_2026-08-30.txt`) and the APK
build. On-device confirmation of v1.1 (versionCode 2) is the remaining gate —
v1.0's whole point is that a green suite did not mean a working app.

---

## Phase 6 — exact stores, price comparison, aisle view (2026-08-30)

User request, three parts: (1) pin each store in the registry to the *exact*
real-world shop, via a search that offers options to pick from; (2) compare
prices, with a brand/product picker so the comparison is about a specific
product rather than "milk"; (3) while **I'm at ⟨store⟩** is active, let the list
be grouped **by aisle** as well as by category.

**Data sources — decided by the user 2026-08-30**, after being offered a
local-history alternative for each:

| feature | source | offered alternative, declined |
|---|---|---|
| store identity | OpenStreetMap (type an area) | Google Places; OSM + phone GPS |
| brand/product list | online product search | own purchase history; LLM guess |
| prices | **online lookup only** | own recorded prices; hybrid |
| aisle | online lookup per store | learned from check-off order; LLM guess |

The online-only price choice was made with the objection on the table — that
per-store retail prices are not reliably available and an LLM reading search
results will sometimes produce a confident wrong number. It is the user's call
and it stands. What follows from it is a build obligation, not a veto: **every
number this feature shows carries its source URL and its fetch date, and "no
price found" is a first-class answer.** The failure mode to design against is
not "we couldn't find it" — it is a plausible figure with nothing behind it.

### The posture change, stated plainly

Until now ThinCart made **no outbound network calls at all**. Everything lived
on the tailnet, and the only non-local dependency was vLLM on `127.0.0.1:8000`.
These three features change that: the DGX will fetch from Nominatim and from
SearXNG's upstream engines, and the queries carry **store names and the things
the household is buying**. SearXNG proxies to public engines, so that content
leaves the tailnet.

That is a real change to what this app is, and it is why this phase is written
down before it is built rather than after. The mitigations are structural:

- **One choke point.** `server/lookup.py` is the only module in the repo
  permitted to make an outbound request. Everything else calls it. Grepping for
  outbound HTTP anywhere else is a review failure.
- **Per-feature kill switches.** `THINCART_LOOKUP=off|stores|all` in the systemd
  unit. Off is a supported state: the app degrades to exactly today's behaviour.
- **Cache before network.** Every lookup is cached in SQLite with its fetch
  date; a repeat question never re-queries. This is a privacy measure as much as
  a latency one.
- **No identifiers.** Queries carry item and store names, never the household,
  the list, the calendar, or anything from `away_days`.

### Shape, following the module conventions already here

`lookup.py` is a bound `APIRouter` like `ideas.py`: handed its connection at
startup, best-effort throughout, and **nothing in the list or its sync may ever
depend on it**. When it is down or off, every endpoint 503s and the rest of the
app does not notice — the same contract the LLM features already keep.

Provenance is not optional anywhere in this phase. Every row any of the three
features writes carries `source` (URL or engine), `fetched_at`, and for prices a
`confidence`. A record that cannot say where it came from is not written.

### 6A — exact store  *(no dependencies; unblocks 6B and 6C)*

- `GET /api/stores/search?q=&area=` → Nominatim, filtered to grocery-ish OSM
  tags. Returns name, address, lat/lon, `osm_id`. Rate-limited to Nominatim's
  1 req/s policy with a real User-Agent, and cached.
- `stores` gains `osm_id`, `address`, `lat`, `lon`, `brand` via the existing
  migration list (`db.py:109`). `store_upsert` carries them.
- Stores panel: **Add store** → name + area → pick from results. Free-text add
  stays, because a shop OSM does not know must still be addable.
- Existing stores keep working untouched; pinning one to an OSM entry is an
  edit, never a migration.

### 6B — aisle view  *(needs 6A: an aisle question is meaningless without a real store)*

- `aisle_hints(store_id, catalog_id, label, source, fetched_at)`.
- `GET /api/aisles?store_id=` → for the items currently on the list, look up
  each one's aisle at that specific store (SearXNG + LLM extraction, long TTL —
  a shop's layout changes on the order of months). Cached per (store, item).
- UI: while **I'm at ⟨store⟩** is set, a segmented control appears above the
  list — **Category | Aisle**. Category is today's view and stays the default.
- Aisle view groups by label in learned walking order where known, and puts
  everything unknown in a final group. **An item with no known aisle is shown as
  unknown, never guessed into a plausible one** — a wrong aisle costs a lap of
  the shop, which is worse than an honest blank.
- With the toggle set to Aisle and nothing known, it falls back to category
  grouping and says why.

### 6C — product + price comparison  *(largest, most uncertain)*

- `products(catalog_id, brand, name, size, source, fetched_at)` and
  `price_quotes(product_id, store_id, price, currency, source_url, fetched_at,
  confidence)`.
- `GET /api/products/search?catalog_id=` → online product search → brand/size
  options to pick from. The pick is remembered against the catalog item, so the
  question is asked once per product, not once per shop.
- `GET /api/prices?catalog_id=&product_id=` → a quote per store in the registry.
- Item sheet gains **Compare prices** → brand picker → a table of store, price,
  date, source. Every screen of it is labelled as looked up online and not
  verified at the shelf; a quote past its TTL is shown greyed with its age, not
  refreshed silently. Tapping a row sets the preferred store, which is an
  existing mechanism.
- The currency display is already symbol-free (`fmtYen` renders 💰 + number), so
  no locale work is needed.

### Test obligation

The seam lesson from the APK (§2026-08-30) applies directly. These features are
the first in this repo whose correctness depends on **something outside the
process**, so the suite has to be split accordingly: parsers, extraction and
provenance rules tested against **recorded fixtures** with no network; the
network path itself tested separately and allowed to be skipped when offline. A
green suite must never again be able to mean "the code is fine" while the thing
it talks to has changed shape.

Explicitly tested: a lookup that finds nothing renders as "not found" and never
as a number; a quote without a source URL is refused at write time;
`THINCART_LOOKUP=off` leaves every existing endpoint byte-identical.

### 2026-09-06 — the data source, found (supersedes the 6B/6C sourcing above)

The plan above assumed web search would answer "what does this cost here" and
"which aisle is it in". It cannot, and two rounds of being wrong about it are
worth recording, because both errors were the same error.

**What actually happened.** SearXNG returned nothing (every general engine it
proxies is CAPTCHA'd or access-denied from this box; `bing` and `seznam` work
and are merely `disabled: true` in its config). With search fixed, the results
for these questions are marketing pages and a cashback app coincidentally
called "Aisle" — better search does not put a price into a page that has none.
The user then pointed at a product page and said the information is visibly
there. It is. **The mistake both times was concluding from the anonymous page
that the data did not exist, when it is store-scoped and fetched after load.**
Price and aisle were never two problems; they are one.

**Where it lives.** wegmans.com runs on Algolia (app `QGPPR19V8V`, public search
key in the JS bundle). The `products` index holds **one record per (product ×
store)** — 14.1M records — and `filters: "storeNumber:<n>"` scopes it. Verified
2026-09-06 for Princeton, store 93 (from `/stores/princeton-nj`, which carries
`storeNumber":93`):

| field | example |
|---|---|
| `price_inStore` | `{"amount": 6.99, "unitPrice": "$0.44/ounce", "channelKey": "93-Instore"}` |
| `planogram` | `{"aisle": "14B", "aisleSide": "L", "section": "11", "shelf": "1"}` |
| identity | `skuId` 44442, `consumerBrandName`, `consumerSubBrandName`, `packSize`, `upc` |
| stock | `isSoldAtStore`, `isAvailable` |

The same sku across stores: 93 → $6.99, 111 → $7.99, 139 → $4.99, each with its
own aisle. So price comparison is real rather than notional, and the aisle is
per store rather than a generic guess — which is exactly what made the
"learn it from check-off order" fallback worth avoiding.

Note `products_v2` is the store-agnostic index: no price, no `skuId`, and a
`planogram` keyed by an opaque store ordinal. `products` is the one to use.

**Consequences for the build.**

- **A store may or may not have a price source.** This is a Wegmans adapter, not
  a general capability. `stores` gains `chain` + `chain_store_id`; a store
  without them simply has no prices or aisles, and every screen must read
  correctly in that state rather than looking broken.
- **The keys are theirs and can rotate without notice.** The failure mode is a
  visible "prices unavailable", never a stale number presented as current and
  never a blank that reads as "free". Each quote keeps its `fetched_at`.
- **The ToS question is the user's, taken knowingly** (asked and answered
  2026-08-31). Volume stays low: cache first, one request per (sku, store), and
  the whole path sits behind `THINCART_LOOKUP` like everything else in Phase 6.
- The aisle is richer than planned — side of aisle, section and shelf, not just
  a number. Worth showing, since "14B, left, section 11" is a findable
  instruction and "aisle 14" is not.

### 2026-09-06 — gate findings on Phase 6 (what was accepted, what was not)

Nine findings across four rounds. Eight were real and are fixed; the ninth was
wrong and is recorded here so it is not "fixed" by a later reader.

**Accepted.** The price request raced the product pick (`enqueue()` is
optimistic, so the first comparison after choosing priced whatever the generic
name matched — the sku now travels with the request instead of being read back).
Repinning a store kept the old branch's chain link, which for a chain whose
branches share a name meant Princeton's prices under Bridgewater's address. The
2-day price TTL never applied, because price-bearing records were cached under
the 14-day product kind — `cache_get` now takes a `max_age` so one record can be
fresh enough for an aisle and too old for a price. Quotes reported the response
clock as `fetched_at`, making a stale price read as current. Aisles sorted as
text, so aisle 10 walked before aisle 2 — the one thing the view promises. The
aisle map never refreshed, so an item added mid-walk stayed in "unknown" until
you changed shops. Out-of-stock quotes sorted among buyable ones and could be
picked as the cheapest. And every quote said `source: "wegmans"` where the
contract in `lookup.py`'s own docstring promises a source that can be **looked
at** — now the product URL, rendered as a ↗ on each row.

**Rejected, with evidence.** [P1] "the Algolia multi-query payload is invalid;
`/1/indexes/*/queries` needs `{indexName, params}` URL-encoded, so uncached
aisle lookups return unscoped or empty results." Checked against the live API:
the flat shape returns HTTP 200 with the filter applied — every hit came back
`storeNumber: "93"` with aisles 14B and Dairy, identical to the single-query
path, and the params-encoded form returns the same rows. Algolia accepts both.
The uncached path had already been exercised end to end before the review.

A fifth round found two more, both consequences of an incomplete fix in the
fourth: passing the sku to `/api/prices` was not enough, because the SEARCH TERM
was still the item's generic name — the picked jar need not appear in the top
hits for "sunflower butter", so an exact quote could still be replaced by an
approximate one. The picked name now travels with the request too. And picking a
product while the aisle view is open left the aisle map keyed on store + item
ids, neither of which changed, so the old approximate aisle stayed on screen;
the map is now dropped on a pick.

A sixth round found the sharpest one. `wegmans_products_many` returned its
partial cache as a success when the network leg failed, so items nobody could
ask about arrived at the UI indistinguishable from items that genuinely have no
aisle — and were rendered as "Aisle unknown". That is rule 2 broken by the
module that declares it. It now returns `(records, complete)` and the endpoint
reports `partial`, which the UI says out loud: *those are not "unknown", just
unasked.* The same round asked for the provider's app id, index and store URL to
be configurable rather than baked in; accepted in substance (all four are
theirs, not ours, and can change without notice) though not in its framing —
this repo has no `.env`, and its configuration lives in the systemd unit
alongside THINCART_TZ and THINCART_LOOKUP, which is where these went.

A seventh round caught the same race a third time, now in the aisle path:
choosing a product fired `/api/aisles` alongside the optimistic pick, so the map
could be rebuilt from the OLD generic name and cached under an unchanged key,
pinning the stale aisle. The fix removes the request rather than sequencing it —
`/api/products/search` now returns each option's aisle label, so choosing one
updates the aisle view from the record already in hand. Three findings in a row
came from the same source: **the op queue is optimistic, so anything read back
from the server immediately after an `enqueue()` is racing it.** Where a value
is already known client-side, use it; where it is not, send it with the request.
Never read it back.

An eighth round found three, and the first showed the seventh's fix was half
done: a global `partial` flag still left the UI unable to say WHICH items went
unasked, so it grouped them under "Aisle unknown" anyway — the very confusion
the flag was added to prevent. The endpoint now names them, and they get their
own group ("Not looked up — try again"). Also: an empty list made
`_algolia_multi` return None for an empty request set, so opening the aisle view
with nothing on the list reported the lookup as unavailable. And every 503 was
rendered as "lookup is off", though 503 also covers a missing key or a provider
failure — one is a setting to change, the other is weather to wait out, so the
body now carries `lookup_disabled` vs `lookup_unavailable` and all three screens
branch on it.

A ninth round found the last one: choosing a product with no aisle data left the
previous approximate entry in place, so the generic match's location was then
shown as the chosen product's exact one. The entry is now deleted.

A tenth round caught the list-sync gap: the aisle map is keyed on the list, but
only `loadAisles()` evaluated that key, and websocket state updates call
`render()` alone. An item the other phone added while you were walking therefore
sat in "Aisle unknown" for the rest of the trip. Both state paths now call a
`syncAisles()` that reloads when the key has moved and no-ops otherwise.

An eleventh round found two more. Overlapping `loadAisles()` calls could land
out of order — change shops mid-request and the older reply, arriving last,
would put the previous store's aisles on screen under the current store's name;
responses are now discarded unless the key they were fetched for is still the
one on screen. And `/api/products/search` still searched the generic name even
when a pick existed, so a remembered product outside the top hits came back
absent from its own options list: shown as chosen, impossible to see.

A twelfth round found the deepest one, and it bites the very case this phase
exists for. Stores are keyed by canonical NAME, so pinning a second Wegmans
collapses both branches into one row: the second pick overwrites the first
branch's address and clears its chain link, and comparing one product at two
branches — the obvious use of price comparison when only one chain has an
adapter — becomes impossible. Rather than re-key store identity on `osm_id`
(which ops reference by name throughout, and would ripple into checkoff, edit
and the "I'm at" selector), the OSM picker now names a colliding branch after
its town: *Wegmans (Bridgewater)* beside *Wegmans*. The town comes from
Nominatim's structured address, which we already request, rather than being
picked out of the display string. Re-keying identity properly is banked, not
done; this makes the two branches coexist, which is what the feature needed.

A thirteenth round found the last two, both about a change made on the OTHER
phone. The branch-clash check compared names with `===` while the server keys
stores by `canonical()`, so a free-text "wegmans" followed by an OSM "Wegmans"
missed the clash and repinned the existing row — the check now uses `canon()`,
the client twin of that function. And the aisle key held store + item ids but
not product picks, so a pick made on the other phone never moved the key and
this phone kept showing the old product's aisle indefinitely; `state()` now
carries a compact `catalog_id -> sku` map and the key includes the picks for
items actually on the list.

Rounds fourteen and fifteen closed the last two. Picking a product cleared its
aisle entry but left it in the "unasked" set, so an item we HAD looked up and
which genuinely has no aisle was reported as "Not looked up". And only the
success path of `loadAisles()` checked whether its reply was still the current
one — an older 503 or a thrown error landing after a newer success would wipe
the current store's aisles and blame the wrong thing; the guard now sits on
every exit, `catch` included. The Nominatim endpoint also became overridable,
for the same reason the chain identifiers are: it is somebody else's URL.

A sixteenth round found the same distinction broken at its own boundary: when
EVERY priced store failed, `/api/prices` returned 200 with an empty quote list,
so the UI's empty-check fired first and said "no price found" — a claim about
the shop when the truth was an outage on our side. It now 503s when something
broke and nothing came back, keeping `partial` for the mixed case.

A seventeenth round made the best structural point of the review: **the default
should be `off`, not `all`.** The contract before Phase 6 was no outbound
request at all, and that is what an unconfigured launch should still do — a dev
run, a test process, somebody cloning this public repo. Reaching outside is now
something a deployment opts into, and the systemd unit does
(`THINCART_LOOKUP=all`), so the live app is unchanged. This also surfaced a test
isolation bug: `MODE` is read once at import, and whichever test module imports
the server first fixes it for the process — so the opt-in moved to
`tests/conftest.py`, which runs before any of them.

An eighteenth round caught the contract broken in the one place it was written
down: store-search records carried `source` but not `fetched_at`, though the
module docstring promises both on every record. Stamped at parse time and
carried through the cache.

A nineteenth round found two more, one of them nasty. `THINCART_LOOKUP=stores`
promises OpenStreetMap and nothing else, but *Link prices* went on to fetch the
chain's branch page — stepping outside the mode the owner chose, quietly. And a
branch page that returned 200 without a store number (a redesign, an
interstitial, a bot wall) was cached as `""` under the 90-day chain TTL, turning
a failure to READ into a durable "there is no such branch" that would not retry
for three months. Both fixed: linking now requires a content mode, and only a
validated number is ever cached.

A twentieth round closed the branch-collision fix properly: Nominatim does not
guarantee a town on every result, and with an empty one the name fell back
unchanged and the collision returned. A clash must always produce a different
name, so it now falls through town → street → osm_id. The last is ugly and
always present, which is the right trade when the alternative is silently
overwriting the other branch.

A twenty-first round overturned a deliberate design decision, and was right to.
Approximate matches were being SHOWN in the price comparison, marked with a ≈
and a footnote. The objection: a price is a claim about one product, and putting
a different jar's amount into the same price-sorted list implies a
comparability that marking the row does not undo — the cheapest line could be
something the household does not buy. Once a product has been picked, a near
match now leaves the comparison entirely and appears beneath it under "Not
listed there — what they have instead". Before a pick there is nothing to be
approximate *to*, so the best match on the item's own name stays the answer.

This is the one finding that changed a decision rather than fixing a slip, and
it is the same lesson as all the others arriving from a new direction: marking
a value as uncertain is not the same as not presenting it as certain. The layout
was doing the asserting.

A twenty-second round caught a bug introduced by the twenty-first: the quote
dict was named `row`, shadowing the catalog row it was built from, so the SECOND
priced store raised `KeyError` — i.e. the comparison feature crashed in exactly
the case comparison exists for, and only there. There is now a test with two
priced stores, driven from the cache so it needs no network. The same round
noted `zip(strict=False)` would silently drop terms if Algolia returned fewer
results than queries, reporting `complete=True` over a short list; the count is
now checked.

Codex reached LGTM on the twenty-third round; the remaining block was the
600-line ceiling, `lookup.py` having grown back to 639. Split again along the
seam that matters: the HTTP surface moved to `lookup_api.py`, and **lookup.py
remains the only module in this repo that makes an outbound request** — the
handlers ask it for facts and shape them into responses. So the outbound surface
is still auditable by reading one file, which was the point of concentrating it.

**Rejected, with reasons.** [P2] "make the provider's app id, index and endpoint
REQUIRED env values and report the adapter unavailable when any is absent,
rather than falling back to constants." Declined. The key alone already gates
the adapter: unset means no prices and no aisles. Making three more variables
mandatory adds deployment steps for no correctness gain — and if the provider
does change an identifier, the stale default produces a failed request, which
surfaces as a visible "couldn't reach", not as a silently wrong answer. The
overridability the previous round asked for is in place; requiring it as well
would trade a working default for friction. (The premise that this repo has a
`.env` "source of truth" is also wrong, and was checked rather than assumed:
there is no `.env` in the tree, nothing in the code or docs references one, and
configuration is four `Environment=` lines in `server/deploy/thincart.service`.
The same finding arrived three times across rounds 8, 10 and 12; the answer did
not change.)

**The pattern worth keeping.** Nine rounds, thirteen real findings, and almost
every one was the same mistake wearing a different hat: *a value presented as
more certain than it was.* A stale price as current. An approximate match as the
product. An unbuyable one as the cheapest. An unasked item as one with no aisle.
A provider outage as a setting. A previous product's aisle as this one's. The
phase was designed against exactly this and it still got in thirteen times,
which is worth remembering next time the design feels like enough.

Three of them shared one mechanical cause worth stating on its own: **the op
queue is optimistic, so anything read back from the server immediately after an
`enqueue()` is racing it.** Where the value is already known client-side, use
it; where it is not, send it with the request. Never read it back.

## 2026-09-07 — the week's TODOs, and the "done but not on the phone" problem

From the 2026-09-06 weekly report. Its headline is the one that matters: *Claude
Code reports features complete, the phone disagrees, and this has now happened
twice.* That is a true description of the last two weeks and the cause is not
carelessness about testing — the suites were green both times. It is that the
suites covered a layer **next to** the one the phone runs.

- The APK: the launcher's JavaScript was tested in jsdom; the bug was in
  Capacitor's native navigation policy, which no jsdom test can reach.
- Phase 6: the server was verified end to end against the real chain data;
  `app/index.html` — the file the phones actually execute — had **no automated
  tests of any kind**, and every bit of Phase 6's aisle grouping, price
  rendering and error branching lives there.

So the structural answer is `app/tests/`, a jsdom suite that loads the real
served page, seeds real state, and drives what the phone would draw: 29 checks
across the aisle view and the price sheet. It cannot press a button on a
handset and does not pretend to. What it removes is the specific failure where
the served page is broken and everything is still green.

Every rule it pins was a review finding on the server that the client could
silently undo — the server can separate a near match from an exact one, refuse
to call an outage an absence, and stamp each quote with its age, and none of
that reaches the shopper unless the page renders it that way.

### The four TODOs

**Icons (絵文字のバックフィル) — done, live.** 139 of 257 catalog rows had no
icon and were *stranded*, not merely missed: `sweep()` only revisits rows with
`llm_enriched_at IS NULL`, so anything enriched before per-item emoji existed
was never looked at again, and the curated map does not carry specific
real-world names ("Amys frozen pizza", "grass fed 2% milk", あさり).

Fixed at the root with `catalog.backfill_emoji()` in the nightly sweeper.
Emoji-ONLY by design: re-running `enrich()` would refill category, edibility and
plants from the LLM and quietly overwrite a category set by hand in the item
sheet, which is a worse outcome than a missing icon.

The first attempt filled **zero**, which is the useful part of this entry. The
obvious prompt — "a single emoji that best pictures this; null if none fits" —
returned null for *everything*, Greek yogurt included: given an escape hatch and
no encouragement, the model takes the hatch. Naming the fallback explicitly
(yogurt → 🥛, paper → 🧻) and reserving null for gibberish turned it around.
Measured against the live model before shipping, not after. 138 of 139 filled;
the holdout is `cau`, correctly declined. **256/257 now have icons.**

Gate finding on that fix, worth keeping: the backfill selected blank rows
`LIMIT 30`, so a batch of names the LLM rightly refuses would be re-selected
every run and starve everything behind it — a queue that looks busy and never
moves. `item_catalog.emoji_tried_at` now records the ATTEMPT whether or not an
icon came back, untried rows are served first, and a decline goes to the back to
be retried only after every other row has had a turn (a later model, or an
edited name, still gets another chance).

**Store recommendation rules (推薦ルールの検証) — verified.** The rule was
already correct; half of it had no test. Added: tie-break by recency (equal
counts → where you last bought it), frequency outranking recency (three visits
beat yesterday's one-off — otherwise a single unusual trip rewrites the regular
answer), and a deleted store dropping out of the recommendation. The subtlety
now pinned is that `recommended_stores` orders ASCENDING by (count, last) and
lets dict overwrite pick the winner; inverting that ORDER BY silently inverts
the answer. Note the live DB has 34 store-stamped purchases, all at one shop —
so this rule has never actually been exercised in production.

**Travel-day detection (旅行日検出の自動化) — code complete, blocked on one
browser step.** Verified rather than assumed: `~/.config/thincart/google_oauth.json`
holds `client_id`/`client_secret`/`calendar_ids` and **no refresh_token**, so
`is_linked()` is false and the 6-hourly sweeper idles — no calendar lines in the
journal for seven days. `--authorize` was run far enough to confirm it prints a
valid Google URL with PKCE, so the credentials are good and the flow is not
broken; it needs a Google sign-in nobody but the owner can do. The app already
says so in the Travel panel. The sweeper re-checks `is_linked()` every cycle, so
authorising takes effect within 6 h with no restart.

**Phone verification (実機確認) — see above,** plus a short checklist handed to
the owner so the check is two minutes rather than open-ended.

### Corrected in passing

Nightly backups looked dead — `~/backups/shopping-list/` stops in July. They are
fine: the timer is enabled and active, ran 3 h before this was written, and
writes to `~/backups/thincart/`, which holds 28 dailies including today. The
July directory is the pre-rename path. Checked rather than reported.

### 2026-09-07 (later) — why the phone kept showing the old app

The owner asked why updates were not appearing. Cause found, and it is almost
certainly the whole of the recurring "reported done, phone disagrees" complaint
rather than a coincidence alongside it.

**`/`, `/sw.js` and `/manifest.json` were served with no `Cache-Control` at
all.** Absent that header an HTTP cache falls back to HEURISTIC freshness —
conventionally about a tenth of the age since `Last-Modified` — so a WebView can
serve yesterday's page for hours without asking. The service worker does not
rescue it: `sw.js` is deliberately network-first, but its `fetch()` goes through
the very cache that is answering stale. So the deploy was real, the tests were
honest, and the phone was reading a copy from before any of it.

Fixed at the source: a middleware sets `Cache-Control: no-cache` on those three
paths. `no-cache` means *revalidate*, not *do not store*; with the ETag
FileResponse already sends, an up-to-date phone gets a 304 and a few bytes.
Icons are left cacheable on purpose — named by content, changed about never, and
making each revalidate would cost a round trip per item for nothing. The service
worker's cache name went v7 → v8 so any shell already sitting in Cache Storage
from before the fix is evicted on activate.

**And made visible.** `/health` reports the stamp of the page the SERVER holds,
the page carries the stamp of the file IT came from, and the Stores panel shows
both — turning red with "this phone is on X, the server has Y" when they differ.
The thing that made this last for weeks was not the caching; it was that a stale
shell looked exactly like a working one. Now it announces itself.

The stamp is the file's content hash, **injected as the page is served**, not a
constant in the source. The gate caught the first version, which was a
hand-maintained `const BUILD = '2026-09-07a'`: the one time a bump is forgotten,
a stale phone and the server report the same value and the warning confidently
declares an outdated client current — precisely when the check is needed. A hash
cannot be forgotten, because changing the file is changing the hash. Serving via
substitution costs an ETag, so it is set to that same hash and a current phone
still pays only a 304. `/index.html` is routed through the same handler, since
left to the static mount it would serve the placeholder untouched and tell that
client it was stale forever — a warning that is always wrong is worse than none,
because you learn to ignore it.

Pinned by tests, because a missing header regresses in total silence and leaves
no trace anywhere else. The whole concern — the middleware, the stamp, the index
route — moved into `server/shell.py` when app.py crossed the 600-line ceiling;
it is one idea (serve the page, and never a stale copy) and reads better named.

### 2026-09-07 (later still) — a way to actually reload

The previous entry found why the phone kept showing the old app and fixed it at
the server. The owner then reported the obvious follow-up: *"I still cannot
reload the app. Why am I not seeing an update button like I do in OutfitAdvisor?"*

Both halves of that have answers, and they are different answers.

**Why there is no update button, and should not be.** The two apps are built
opposite ways round. OutfitAdvisor ships its UI *inside* the APK (`webDir: www`,
no navigation out), so a new UI means a new APK, and it needs the whole delivery
channel it has: `server/updates.py`, `publish_apk.py`, a native `AppUpdatePlugin`
and an "Install update" button. ThinCart's APK is a launcher — it probes the
saved server and hands the WebView to the live page. There is nothing to
install. That asymmetry is deliberate and is why changing the list UI here needs
no APK at all.

**Why reloading did not work.** There was no way to do it. `app/index.html` had
no reload control of any kind — no button, no pull-to-refresh, no touch handlers
— while the stale banner told the owner to "pull down / reopen". Neither gesture
exists: a Capacitor WebView has no pull-to-refresh, and a reopen re-navigates to
a URL the HTTP cache is still entitled to answer from its own store. So the
banner could be right, the advice followed exactly, and the phone stay stale.
The only remaining exit was Android's app-storage screen, which is what the
owner had to use.

Worse, the 2026-09-07 fix could not reach the handset by itself. `Cache-Control`
binds only copies fetched *after* it shipped; an entry stored under the old
rules keeps its heuristic freshness. `/sw.js` was cached the same way, so the v8
worker that would have evicted the stale shell could not install — the old
worker answered requests for its own replacement out of the very cache that was
stale. A fix sitting behind the cache it was written to defeat.

Three doors, so three keys:

- **`reloadApp()` in the page** — a button that is *always* visible, not only
  when staleness is detected (OutfitAdvisor learned that one the hard way: a
  dismissible banner let a phone sit three versions behind). It fetches `/` with
  `cache: 'reload'` to bypass the freshness check and rewrite the stored entry,
  calls `registration.update()` so the worker fetches its own replacement, then
  reloads.

  **The safety property is that nothing is ever torn down, and it is what the
  tests pin.** On a failed fetch nothing happens at all: no update, no reload,
  and an error saying the copy in hand still works. On success the eviction is
  done BY the new worker — populate in `install`, evict in `activate` — so it is
  atomic with having somewhere to evict to and never runs if the install could
  not complete. If the update itself fails we stay on the old worker, which is
  an old app rather than no app.

- **`fetch(e.request, {cache: 'no-cache'})` in the service worker** — the root
  fix, and the one that makes the ordinary case need no button at all. The
  worker was already "network-first", but a plain `fetch()` consults the HTTP
  cache first, so a heuristically-fresh entry answered ahead of the network and
  network-first was first in line behind a hit. `no-cache` revalidates: a
  conditional request goes out on every navigation the worker sees, an unchanged
  page comes back 304, and a dead zone still falls through to the cached shell.
  The staleness lived here as much as in the missing response headers.

- **"Force a fresh copy" on the launcher's settings screen** — navigates to
  `/?fresh=<ms>`, an address never requested before, which is the one way past a
  cache entry with certainty rather than by asking it nicely. Not the default:
  it forfeits the 304 on every launch. This is the door that still opens when
  the served page is so old it predates the ↻ button inside it — the case that
  previously had no answer but Android's settings.

`sw.js` went v8 → v9, revalidates as above, and now stores under a
query-stripped key. The reads
already used `ignoreSearch`; without the writes matching, every forced refresh
would have left a permanent extra copy of the shell that nothing ever read.

The reload goes through a named `hardReload()` seam for the same reason the
launcher's navigation does — jsdom locks `location.reload`, and without a seam
the branch deciding *whether* to reload cannot be tested, which is the entire
safety property.

Suites: **193 python** (+1: the cache-buster URL must still route to the stamped
page, not to the static mount, which would serve the placeholder and tell that
client it was stale forever), **71 web** (+42, `app/tests/reload.test.js` and
`app/tests/sw.test.js`), **52 launcher** (+6). One existing launcher test needed
correcting rather than re-baselining: it stubbed `setTimeout` with a single fixed
id that collided with jsdom's own counter, so the probe's ordinary tidy-up read
as the refusal timer being cancelled.

**One [P1] from the gate, and it was right.** `sw.js` cached whatever the
network returned, including a 4xx/5xx — those arrive on the success path,
because an error *response* is a reply and not a failure. The reload button
makes that reachable deliberately: it asks the server for the shell, and on a
bad answer it reports that nothing was changed and keeps the copy in hand. That
promise was false, because the worker had already overwritten the offline shell
with the error page on the way past, and the next dead zone would have served
it. Now only `res.ok` is stored, and the write is held open with `waitUntil` so
it cannot be cut short by the worker being killed the moment the page has its
bytes.

The finding also exposed that **`sw.js` had never had a test of any kind** —
this file is the one that held the staleness in place for weeks, and the gate
was its first reader. `app/tests/sw.test.js` now drives it in a vm context with
`self`, `caches` and `fetch` stubbed: good responses update the shell, error
responses do not, a dead network falls back, three forced refreshes leave one
entry rather than three, `/api` and `/ws` are never touched, and activate evicts
every older cache. Web suite **67**.

**A second [P1], also right, and it made the design simpler.** The first draft
of `reloadApp()` cleared Cache Storage and unregistered the workers before
reloading. Fetching the page first meant it could not strand a phone whose
tailnet was already down — but connectivity dropping in the window between that
fetch and the reload would have left no offline shell AND no worker to serve
one, on a page whose own `no-cache` headers make the reload ask the network
again. A window of milliseconds, on a phone radio, in a shop, with the app gone
until signal returned.

The fix was to stop doing it from the page at all. `registration.update()`
refetches `sw.js` past the cache, and the new worker fills its cache in `install`
and drops the old ones in `activate` — the eviction that was wanted, done where
it cannot leave a gap. Fewer lines than the version it replaced. Both of the
gate's findings were about the same thing from opposite ends: the offline copy
is the thing that must survive.

Suites at the end: **193 python**, **70 web**, **59 launcher**.

**A third finding, [P2], and the most useful of the three.** Between the two
drafts above, the launcher warmed the shell with a cross-origin `cache:"reload"`
fetch before handing over. Chromium partitions the HTTP cache by top-level site:
a fetch issued while the launcher is still on its own Capacitor origin refreshes
a partition that the subsequent top-level navigation never reads. It would have
looked like it worked and changed nothing — the worst shape of bug this project
keeps meeting, and the same shape as the original: a fix that tests green next
to the layer it was meant to fix.

Removing it forced the better question — where does freshness actually belong? —
and the answer was the service worker, which runs on the server's own origin and
sees every navigation. One argument to `fetch()`, and the mechanism `shell.py`
has described in prose since yesterday is finally closed in code: *"sw.js is
network-first, but its fetch() goes through the very cache that is answering
stale."* It did. Now it does not. The launcher keeps only the cache-buster, which
needs no assumption about partitioning because no partition has ever seen the
address.

`sw.js` had never been tested, and all three findings were in or about it.

Suites at the end: **193 python**, **71 web**, **52 launcher**.

### 2026-09-07 (evening) — ⚙️ Settings, and the reload moved into it

The reload button worked; the owner could not find it. *"I don't see it."* It
had shipped at the foot of the Stores panel, below the store rows, the add-store
row, the store search and its results — visible only after scrolling to the
bottom of a panel opened for an unrelated reason.

That is a placement bug of a specific kind: the control answers "the app is
showing me the wrong thing", so it has to live where someone would look while
thinking exactly that. Nobody thinking it opens 🏬.

So there is now a **⚙️ Settings panel**, built like every other panel in this
file, holding the version block that was buried: the build line, the ↻ button,
its error line, and one sentence saying what reloading does and does not touch
(the list and anything queued offline are untouched — the question anyone would
have before pressing it). `showBuild()` re-runs when the panel opens, so the
comparison is made when it is read rather than whenever the language was last
applied.

Nothing about the reload mechanism changed, and no APK is needed: the launcher
is untouched and this is all in the served page. Web suite **81**.

The general lesson, and the second time this session has produced it: a feature
verified only at the layer below the one the owner touches is not verified. The
suite proved the button fetched, updated the worker and reloaded, in the right
order, under failure. None of that says anyone can find it.

### 2026-09-07 (evening) — "I somehow cannot delete the stores"

Not the server, and not the op. `apply_store_delete` is correct and covered,
the op left the queue, the row left the database, the new state came back. The
**panel never redrew**, so the store stayed on screen and delete looked broken.

`render()` live-refreshes the open Stores panel behind a guard, so it cannot
rebuild the DOM under someone typing a note. The guard was:

```js
!$('stores-panel').contains(document.activeElement)
```

On Android, tapping a `<button>` focuses it. So from the moment Delete was
tapped, that button held focus and **every** later render was suppressed — the
one from `enqueue()`, the one after `resync()`, and every WebSocket frame after
that. The row only disappeared when focus moved: closing and reopening the panel
rebuilt it from scratch, which is why it read as intermittent rather than
broken.

The guard protects typed text, so it should test for typed text:

```js
focused.tagName === 'TEXTAREA' || focused.tagName === 'INPUT'
```

Notes, the new-store field and the store search are all protected exactly as
before; buttons no longer freeze the panel. One occurrence in the file, checked
rather than assumed.

`app/tests/store_delete.test.js` drives the real paths — a click on the real
Delete button, a WebSocket frame for the other phone's change — because the bug
lived in precisely the wiring a convenient stub would have skipped. Verified to
fail without the fix: 3 of its 13 checks go red, including "the row is gone from
the panel". Web suite **94**.

**Third time this session, same shape.** The stale shell, the unfindable reload
button, and now this: each was correct at the layer that had tests and wrong at
the layer the phone touches. `app/tests/` exists for that reason and is the
right place for the next one too.

**A [P2] from the gate on the same diff, and a framing I do not accept.** It
reported that narrowing the guard *introduced* a defect in the price-link
button: `renderStores()` can now run while `/api/stores/link` is in flight,
detaching the `lk` element the handler captured, so a failure reason is written
where nobody can see it and the rebuilt button comes back enabled and re-fireable.

The mechanism is real. "Introduced" is very probably wrong: the handler's first
act is `b.disabled = true`, a disabled element is not focusable, and Chromium
moves focus off it — so by the time the request was even sent, the old
focus-based guard had already stopped protecting anything. jsdom does not model
disable-blurring, so this could not be settled here, and the defect is real
under either reading. Fixed rather than argued.

The fix is the general one: **state that must survive a repaint does not belong
in the DOM being repainted.** `linking` (a Set of store ids) and `linkFail` (id →
reason) live beside the panel and `renderStores()` draws them. While a lookup is
out there is no button at all, so no rebuild can hand back a fresh one and let
the same request go twice. The reason now sits beside the button instead of
replacing it — most are "this branch isn't in their index", worth saying and
worth being able to retry without reopening the panel.

Pinned by two more sections in `app/tests/stores_panel.test.js` (renamed from
`store_delete.test.js`, since it now covers the panel's refresh generally): a
link held open across a rebuild keeps saying it is running, offers nothing to
press twice, and still shows its answer afterwards. Verified against the
pre-fix file: 5 of the 20 checks go red. Web suite **101**.

**A further [P2] on the same button, and this one had no argument against it.**
On success the pending flag was cleared as soon as the upsert was *queued*.
`enqueue()` is optimistic — it returns the moment the op is on the queue, and
`base.stores` shows no link until the round trip completes — so the Link button
came straight back, enabled, offering to run the same lookup again. Offline,
where an op can sit queued for hours, it would offer that on every draw.

`linkOpt` (store id → the branch number the lookup returned) now holds the
answer until the server's own value appears, at which point the local one is
dropped. The row reads as linked from the instant the lookup succeeds, which is
also what someone would expect to see. Web suite **106**.

**And a [P1] on the fix to the fix.** The link handler's own `renderStores()`
calls walked straight past the typing guard: start writing a note while the
lookup is out — which is exactly when you would, since it is slow — and the
answer arriving rebuilt the field and took the text with it. The guard had just
been repaired and was being bypassed three lines away.

There is now **one** way to redraw an open panel, `refreshStores()`, and every
redraw that is not the panel opening goes through it. A redraw it skips is not a
redraw lost: everything the panel shows — `base`, `linking`, `linkFail`,
`linkOpt` — lives outside the DOM, so the next render paints the same truth.
That is what keeping state out of the DOM buys, and it is why the earlier fix
made this one two lines. Web suite **108**.

Known and left alone: tapping an "I'm at…" chip redraws the panel synchronously
and will discard a half-typed note. Pre-existing, the same class, and a
different change — noted rather than folded in.

### 2026-09-07 (evening) — what Whole Foods and ShopRite actually expose

The owner asked for price linking at Whole Foods and ShopRite as well as
Wegmans, plus shelf location for both. Probed live before designing anything,
because the whole feature turns on what those chains publish.

**Whole Foods.**

| question | answer |
|---|---|
| branch id | **yes** — `GET /stores/<slug>` carries `"storeCode":"10187"` |
| per-store price | **yes** — `GET /api/products/category/<cat>?store=<code>` returns `{name, brand, regularPrice, uom, slug}` |
| free-text search | **no**, not over plain HTTP |
| shelf location | **no** — nothing anywhere |

Verified: store page for Princeton → `10187` → that code accepts product queries
and prices come back (`Organic White Onion $3.19/lb`). The store-code flow is an
exact parallel of the Wegmans branch-page flow, so it fits the existing shape.

The search is the problem. The category endpoint **ignores** `text=`, `keyword=`
and `q=` — it returns the category listing whatever you pass, which is why an
early probe "found" onions for a milk query. That is precisely the failure this
repo's rule 2 exists to prevent, caught because the result was read rather than
counted. The real search endpoint is `/api/wwos/rsi/search` (found in their JS
bundle; `old` is a required parameter). It answers 200 with the right envelope
and **zero** results without session state that a cookie jar plus
`/api/session-id` did not reproduce; `/api/store-affinity` is POST-only.

Shelf location does not exist to be had: no `aisle`, `shelf`, `planogram`,
`department` or `location` field in the category API, the product record, the
product page or the store page. **Wegmans remains the only chain with planogram
data**, and the aisle view stays Wegmans-only. Owner agreed, 2026-09-07.

**ShopRite.** Every server-side request — `shoprite.com`, a store storefront
path, and `storefrontgateway.shoprite.com` — returns 403 behind Cloudflare's
"Just a moment..." JS challenge. Nothing is reachable with an HTTP client.

**Consequence for the design.** Owner chose a headless browser for ShopRite
(2026-09-07). The Whole Foods finding pushes it further than expected: WF needs
one too, because the app's flow is *type an item name → search the chain's
catalogue → pick the product*, and without free-text search WF cannot answer
that at all. So a browser-backed adapter serves BOTH new chains, while Wegmans
keeps its Algolia path — a browser round trip is seconds against Algolia's
milliseconds, and the aisle walk fires one query per item on the list.

Design consequences that follow from that, to hold to when building:

- Cache hard. `TTL["price"]` is 2 days and every browser query must be answered
  from SQLite first; the browser is a last resort, not a lookup.
- One at a time. A Chromium per concurrent request would take the box down;
  the worker needs a single-flight lock and a hard timeout.
- The kill switch still governs. `THINCART_LOOKUP=off` must mean no browser is
  ever launched, exactly as it means no request is made today.
- `lookup.py` stays the only module that reaches outward, browser included.
- Best-effort as always: the list and its sync never wait on any of this.

### 2026-09-07 (evening, corrected) — Whole Foods DOES publish shelf location

The owner: *"whole foods has aisle location information when selecting the
store."* They were right and the entry above was wrong. Correcting it here
rather than editing it away, because the way it was wrong is the useful part.

**The mistake.** Every probe went at `wholefoodsmarket.com` without a store
selected. In that state the site serves a delivery-oriented experience with no
shelf data anywhere — which is exactly what was found, and was then written up
as "Whole Foods does not publish it". The absence was real; the conclusion drawn
from it was not. A negative result from one configuration was reported as a
property of the chain.

**What is actually there.** With a store selected the site serves a different
tree, `/grocery/...`, and the product page carries:

```html
<div data-testid="aisle-location"> … Located in Dairy … </div>
```

Verified live: Organic Valley Whole Milk at Princeton (store **10187**) →
`$5.99` and **"Located in Dairy"**. Department-level rather than Wegmans'
`14B · left · sec 11`, but a real shelf location and the thing the owner saw.

**Store selection is a cookie, and it can be built rather than negotiated.**
`wfm_store_d8` is base64 of `{"id","name","tlc","path","state","geometry",…}`.
Crafting it for Princeton and sending it with plain `httpx` returns the full
server-rendered page — the response says `"storeName":"Princeton"` and
`"storeId":"10187"`, so the cookie is honoured. **No browser is needed to read a
price or an aisle.**

| capability | route | browser needed |
|---|---|---|
| branch id | `/stores/<slug>` → `"storeCode"` | no |
| store selection | crafted `wfm_store_d8` cookie | no |
| price | `/grocery/product/<slug>` | no |
| shelf location | same page, `data-testid="aisle-location"` | no |
| free-text search | results render client-side; `/api/wwos/rsi/search` answers 200 with 0 hits even with the cookie | **yes** |

**So the browser shrinks to one job: turning an item name into a product.**
Everything after that is cheap HTTP. That suits the existing cache design —
which product "milk" means at a given store is stable, so it is cached under the
long product TTL and the browser is touched about once per item per store, not
once per query. The aisle walk, which fires one lookup per item on the list,
stays HTTP-only after the first pass.

Playwright and a headless Chromium are installed on the box
(`~/.cache/ms-playwright`, 111 MB) and were what found this: the discovery came
from watching a real browser's network traffic, not from guessing endpoints.

**The lesson, and it is the fourth time this session.** Every failure today has
been the same one — checking the layer next to the one that matters. The stale
shell (server tested, phone not), the reload button (behaviour tested, findability
not), the delete (server tested, panel not), and now this (site probed, but not
in the state the owner uses it in). The question to ask first is not "what does
the API return" but "what is the owner actually looking at".

### 2026-09-07 (evening) — ShopRite: through the wall, short of the endpoint

The owner confirms ShopRite carries aisle information too. Playwright was
installed for this and got most of the way.

**What is established.**

- **A real browser clears the Cloudflare challenge.** `www.shoprite.com` returns
  its own title and lands on `/sm/pickup/rsid/3000`. The 403 that plain `httpx`
  gets is not a hard wall, it is a client check.
- **The API is `storefrontgateway.shoprite.com`** — ShopRite runs on Mi9 Retail
  (`mi9cloud.com` assets). Shape:
  `/api/stores/{rsid}/locations/{uuid}/recommendations?HowMany=&RecommendationName=`,
  plus an `/api/v1/stores/{rsid}/…` namespace seen carrying ad impressions.
- **The browser must be the transport.** A `fetch()` issued from inside the
  cleared page is answered 200; replaying the identical URL over `httpx` with
  every cookie the browser held returns **403**. Cloudflare is fingerprinting
  the client, not checking a cookie — so unlike Whole Foods, there is no
  cheap-HTTP path afterwards. That makes ShopRite the expensive chain, and the
  cache the thing that makes it usable.
- **Their product JSON is rich on price**: `priceLabel`, `priceNumeric`,
  `pricePerUnit`, `unitOfPrice`, `tprPrice` (temporary reduction), `wasPrice`.
  No aisle key in the *recommendations* payload — but that is the wrong
  endpoint to expect one in; Whole Foods keeps its shelf location on the
  product page, not in a carousel.

**Where it stopped, and why the method matters.** The storefront SPA would not
render under `chromium_headless_shell` — 264 characters of body while its API
answered normally. Switching to the full Chromium (`channel="chromium"`, new
headless) fixed the rendering (home 5,610 chars, a category page 14,753), which
is worth remembering: the default Playwright build is a stripped one and is
detected. Search results still do not render, and **eight guessed product and
search paths all returned 404**.

That is the second time today guessing endpoints has produced nothing while
observing real traffic produced everything — the Whole Foods aisle was found by
watching a browser, not by inventing URLs. So the next step is to observe rather
than guess, and the cheapest observer is the owner's own browser: DevTools →
Network → search an item → open a product → copy the `storefrontgateway`
request URLs. Two URLs unblock the adapter.

`rsid/3000` may also be the wrong store — it is whatever the site defaults to,
not a branch near Princeton — which alone could explain empty results.

### 2026-09-07 (night) — Whole Foods and ShopRite, prices AND aisles, no browser

Built and live-verified. Both chains link from an OpenStreetMap pin, price the
list, and place it on the shelf — and the headless browser approved for this
turned out to be unnecessary for either. Playwright was what FOUND the answers
(watching real traffic instead of guessing endpoints); the product ships with
`curl_cffi` and nothing else new.

**What was actually in the way, chain by chain.**

*ShopRite* — not a bot wall, a client check. Their gateway
(`storefrontgateway.shoprite.com`, Mi9 Retail) refused `httpx` even with every
cookie a real browser held, because the TLS handshake gives the client away
before a header is read. `curl_cffi` presents Chrome's handshake and is answered
like Chrome. The second obstacle was headers: the gateway 404s every REAL path
unless four headers the site's own app sends are present — `x-site-host`,
`x-shopping-mode`, `x-customer-session-id`, `x-correlation-id`. Eight correct
URLs had been read as wrong ones. The session and correlation ids can be freshly
generated. `/api/stores` lists all 315 branches; `rsid/3000` is the corporate
placeholder the site opens on and it has no shelves.

*Whole Foods* — the store-selected face of the site serves the search page
server-rendered (a 400 KB `__NEXT_DATA__` island, `props.pageProps.productsInfo`)
to a Chrome fingerprint, and the product page's `productLocation` is a plain
string: "Dairy", or "Aisle 7" — **numbered aisles for centre-store goods**, not
departments only, which the first look had not shown. The product page resolves
from the ASIN alone. The `wfm_store_d8` cookie built from the store code is
honoured.

*One more trap, measured:* Amazon answers about half of Whole Foods searches
with a well-formed page listing nothing (`approximateTotalResultCount: 0`), and
the same query a second later with thirty products. A genuine no-result page is
byte-for-byte the same shape. So an empty page is asked again, and one that
stays empty three times is reported **unavailable** — never "the shop has
none", which would sit in the cache for a fortnight and be shown as fact. A term
Whole Foods truly does not stock costs three requests each time; that is the
honest price of not being able to tell the two apart. Found because the first
live aisle walk placed one item of three; the walk now places all three.

**The shape.** `chains.py` recognises a store as a branch of a chain and owns
the one label every shelf position is written with. `wholefoods.py` and
`shoprite.py` are pure parsers driven by recorded responses, exactly as
`wegmans.py` is; `osm.py` took the Nominatim parsing out of `lookup.py` to make
room. `lookup.py` is still the only module that reaches outward for the
household's shopping — `tests/test_chains.py` greps for it, with the three
loopback/travel exemptions named and reasoned — and now dispatches by chain:

- `products(chain, term, store, prefer_sku)` — search, then **place** the top
  hit and the product the household picked. Both new chains keep the position
  on the product record rather than in search, so placing every hit would cost
  a request each; the two that matter cost two, cached under the aisle TTL.
  "Asked, has no place" is cached as an answer; a failed read is not cached and
  is asked again.
- `products_many` — the page-backed chains have no multi-query, so term by term,
  three at a time, reporting what it could not ask. Wegmans keeps Algolia.
- `resolve_branch` — ZIP first for ShopRite, town for Whole Foods, and a reason
  in words when nothing can be named. Never a nearest-guess.

Labels: `Aisle 9 · shelf 7` (ShopRite), `Aisle 14B · left · sec 11` (Wegmans,
unchanged — its shelf field has never been shown and still is not), `Dairy`
(department, any chain).

**Verified live** (test_results/chains_live_2026-09-07.txt): Princeton Whole
Foods → `10187` in 0.4 s; ShopRite Lawrenceville → `500` in 0.2 s; milk $4.39 /
$4.59, peanut butter $2.69 / $2.29, toilet paper $14.99 / $15.99, every one
placed at both stores; a cached re-ask 0.00 s. Suites: **216 python** (+23),
71 web, 52 launcher.

Not done, and said so: the OSM pin for a Whole Foods can resolve to a town whose
store page is not `/stores/<town>` (multi-store cities); that comes back as "no
Whole Foods store page for '<town>'" rather than a wrong branch. Playwright and
Chromium stay installed as an investigation tool only; nothing imports them.

**Two [P1]s from the gate on the first push, both right.**

1. *A sku is not universal.* The phone sends a just-picked product's sku with
   the price request (the op queue is optimistic, so the pick may not have
   landed). With one chain that was fine; with three, a Whole Foods ASIN sent as
   "the sku" was applied to every store, could never match at ShopRite, and
   demoted that quote to "what they have instead" — even where the household
   had already picked its jar there. The request now carries `chain` as well;
   the supplied product is used at its own chain and every other store is asked
   about ITS remembered pick, or the item's own name before one. Pinned on both
   sides: the server test picks at ShopRite, then prices with a Whole Foods
   ASIN and asserts the ShopRite quote stays exact; the web test asserts the
   phone sends `chain=` with the sku.
2. *The state list was Wegmans' trading area.* Ten states, so a Whole Foods in
   Austin would have been "could not read a town from the address". All fifty
   and DC now, with a test that says why.

Suites: **218 python**, 72 web, 52 launcher.

**Second push, two [P2]s, both right.** (1) Laying a newly picked product's
shelf onto a cached search wrote the records back and re-stamped the row, so a
five-day-old price would pass the two-day check as fresh on the next
comparison. The search is now cached once, when fetched, and never written
back — positions live in their own cache and are laid on at every read, which
costs no request. Pinned by a test that plants a five-day-old search, places a
pick, and asserts the row is still stale for a price. (2) ShopRite's
human-checkable link built its query with spaces swapped for `+` and nothing
else, so "Bowl & Basket" — most of their own-brand range — ended at "Bowl".
`quote_plus` now. Suites: **219 python**, 72 web, 52 launcher.

**Third push, two [P1]s, both right — and both the same rule.** *Never a
wrong branch.* (1) ShopRite's town fallback returned the first branch in a
town; Hamilton Township has two. It now returns a branch only when exactly one
is in that town, and otherwise leaves the store unlinked with a reason.
(2) `/stores/<town>` on Whole Foods is ONE store, and a town can have several.
The store page names its own ZIP and coordinates; the pin must now match one
of them — ZIP for ZIP, or within a car park by distance — or the page is
refused as "the branch at <name> <zip>, not this pin". A pin with neither a ZIP
nor coordinates confirms nothing and is refused too. Suites: **221 python**,
72 web, 52 launcher.

**Fourth push, one [P2], right.** Two items can search as the same words and
mean different jars; the aisle walk kept one preferred sku per term, so the
second jar stayed unplaced even when it was among the hits. `prefer` is now a
list per term and every pick under it is placed. Suites: **222 python**, 72
web, 52 launcher.

**Fifth push, one [P2].** The new chains' endpoints were literals; the Wegmans
ones are overridable from the systemd unit. Now `THINCART_WHOLEFOODS_SITE`,
`THINCART_SHOPRITE_GATEWAY` and `THINCART_SHOPRITE_SITE`, defaulting to what
was verified — a redesign or a proxy is a config change, not a code change.

**Sixth push, one [P2], REJECTED with evidence.** The gate asked that the new
endpoints be *required* from a `.env` and fail when absent. There is no `.env`
in this repo (`ls -a` finds none); configuration is `Environment=` lines in the
systemd unit, the Wegmans endpoints use exactly this default-plus-override
pattern with a comment saying so, and §2026-09-06 above records the same claim
rejected once already. A fresh clone that works out of the box is the point of
the defaults. Pushed with `--no-verify`, said so here and in the PR.

### 2026-09-07 (late) — "montgomery wholefoods (new one) is not showing up"

Not us: OpenStreetMap. Nominatim has no Whole Foods in Montgomery Township at
all — the store opened after the map was last drawn — so the 🏬 → 🔍 pin search
had nothing to return. Whole Foods itself knows it: `/stores/montgomery` is
code **10738**, "Montgomery NJ", Skillman 08558, and a product page under that
code prices and places (Lactaid $4.99, Dairy).

So a store added **by name** — which the app has always allowed — can now be
linked through the chain's own directory. The town in the typed name is the
clue (`chains.town_from_name`: "Whole Foods Montgomery" → `montgomery`,
"ShopRite of Ewing" → `ewing`), the chain's store page or list supplies the
identity, and its address and coordinates come back with the link so the phone
writes them onto the row: the bare name becomes a pin. When there IS an OSM
pin the ZIP-or-distance check from the previous entry still applies.

Same rule as ever, never a wrong branch: a bare "ShopRite of Montgomery" is
refused, because there is one in NJ and one in NY and a name cannot say which.
Getting that right needed "Montgomery Township" and "Montgomery" to read as one
town — compared raw, the NJ row's suffix hid it and the name resolved to New
York. Pinned by tests, and the ShopRite by-town match now runs across every
state and must be unique.

`branches.py` took `resolve_branch` out of `lookup.py`, which had reached 619
lines. No network of its own; the judgement about when a chain's answer counts
as THIS store lives there.

**Gate, first push: two findings, both right.** (1) A bare "Whole Foods
Chicago" would have taken whichever branch `/stores/chicago` is, and there is
no directory to prove uniqueness against. So a name-only link is never pinned
unseen: the chain's answer comes back as `confirm` ("Skillman, NJ 08558") and
the phone puts it in front of the person before writing anything. "No" is an
answer, not a failure, and the button comes back pointing at the map. ShopRite
gets the same confirmation for consistency even where the town is unique.
(2) "Whole Foods Princeton, New Jersey" produced `princeton-new-jersey`; full
state names strip now too, and "Whole Foods New York" stays a town. Also found
on the way: the link handler held its reply in a `const`, so replacing a
declined answer threw and read as "could not reach OpenStreetMap". Suites:
**225 python**, 113 web, 52 launcher.
