# PlantCart — shared shopping list with purchase-cycle & plant-diversity intelligence — PLAN

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
| App form | **PWA** served by the backend; both phones "Add to Home Screen". No APK, no store. |
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
  median toward zero); estimate = **median** (robust to one vacation/skip);
  classify into bins: ≤4.5 d → "twice a week", ≤9 d → "weekly",
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
- **Data repair (un-merge):** backup → `~/backups/shopping-list/plantcart-preunmerge-2026-07-12.db`;
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
├─ tests/               # estimator, canonicalization, sync-op unit tests
├─ test_results/
└─ PLAN.md
```
