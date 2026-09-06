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
