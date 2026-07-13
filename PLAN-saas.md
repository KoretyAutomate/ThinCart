# PlantCart → real app service — productization PLAN (branch `saas`)

Turn the single-tenant, Tailscale-only DGX app into a **multi-tenant service**
deployable three ways from one codebase:

1. **Web service** — public HTTPS SaaS, accounts + households.
2. **Google Play** — Capacitor Android wrapper over the same web app.
3. **Apple App Store** — same Capacitor project, iOS target.

> Status: **foundation BUILT & verified on branch `saas`** (2026-07-04). The live
> single-tenant app on `master` (DGX :8123) is deliberately untouched — this work
> was done in an isolated worktree. Divergent productization track, NOT merged.
>
> **Done & verified:** multi-tenant backend (accounts, households, per-household
> isolation), pluggable LLM (Claude/vLLM/none), WS rooms + ticket auth, rate-limited
> auth, account deletion, security headers/CORS. Web client has login/register/join +
> account settings. 22/22 tests pass (all review must-fixes pinned by a test);
> Docker build health-checked green; data-migration script tested against a copy of
> the real master DB (28 events → 1 household, recommendations reproduce). Deploy
> scaffolding (Dockerfile/compose/fly.toml/DEPLOY.md) + mobile scaffolding
> (Capacitor config, Android CI, SUBMISSION.md, PRIVACY.md, local-notifications
> design) in place.
>
> **Not done (needs the user — see "what only the user can do"):** actually hosting
> it, Anthropic key, the two developer accounts + a Mac for iOS, wiring
> `window.PLANTCART_API_BASE` into index.html for the bundled native build (one
> documented change), and implementing the local-notifications native feature.

> **App Store note (user raised it):** neither store bans AI-assisted code. What
> gets apps rejected is (a) "just a website in a wrapper" with no native value —
> we mitigate with offline-first PWA behavior, push notifications, and home-screen
> integration; (b) missing account-deletion (we ship it — Apple 5.1.1(v)); (c) no
> privacy policy (we ship a template). Risk is real but manageable, not a ban.

---

## The core problem: single-tenant assumptions that must break

| Today (master) | Needs to become |
|---|---|
| No accounts — tailnet membership *is* auth | Real accounts (email+password), signed tokens |
| One global shared list | Per-**household** lists; two spouses join one household |
| Server binds private tailnet IP | Bind `0.0.0.0` behind platform TLS; CORS + security headers |
| Local vLLM on `:8000` assumed | Pluggable LLM: **Anthropic Claude** (hosted default) / OpenAI-compatible / none |
| `snoozed_until` on the global catalog | Per-household snooze (catalog stays a shared corpus) |
| One global `revision` + one WS broadcast set | Per-household revision + per-household WS rooms |
| SQLite single file, no config | Same SQLite (WAL) but **all config via env**; Postgres-swappable later |

**Catalog stays global on purpose.** The 173-item seed + LLM plant/category
enrichment is expensive and identical for everyone → one shared `item_catalog`.
Only *per-household state* (list items, purchase history, snoozes) is scoped.

---

## Data model (additions + one migration)

```sql
users(id TEXT PK, email TEXT UNIQUE, pw_hash TEXT, pw_salt TEXT,
      display_name TEXT, created_at TEXT)
households(id TEXT PK, name TEXT, invite_code TEXT UNIQUE,
           revision INTEGER DEFAULT 0, created_at TEXT)
household_members(household_id TEXT, user_id TEXT, role TEXT, joined_at TEXT,
                  PRIMARY KEY(household_id, user_id))
catalog_snooze(household_id TEXT, catalog_id INT, snoozed_until TEXT,
               PRIMARY KEY(household_id, catalog_id))   -- moved off item_catalog

-- items + purchase_events gain: household_id TEXT NOT NULL  (indexed)
-- item_catalog: KEEP (global). snoozed_until column retired (data → catalog_snooze).
-- revision: per household (households.revision), not global meta.
```

Auth: **pbkdf2_hmac-sha256** password hashing (stdlib, 200k iters, per-user salt);
**JWT** (HS256, `PLANTCART_SECRET`) carrying `user_id` + `household_id`, 30-day.
Every `/api/*` except `/api/auth/*` requires `Authorization: Bearer <jwt>`;
the token resolves the caller's household and scopes every query.

WebSocket auth: browsers can't set WS headers → token via `?token=` query param,
validated on connect; the socket joins its household's room only.

---

## API surface (added to the existing op/state endpoints, now household-scoped)

```
POST /api/auth/register {email, password, display_name}  -> {token, household}
POST /api/auth/login    {email, password}                -> {token, household}
GET  /api/households/me                    -> {household, members[], invite_code}
POST /api/households/join {invite_code}    -> {token, household}   (switch/join)
POST /api/account/delete                   -> 204  (GDPR + Apple 5.1.1(v))
GET  /api/state | /api/catalog | /api/cycles | /api/ideas   (unchanged shape, scoped)
POST /api/op   (unchanged ops; household inferred from token)
WS   /ws?token=...   (joins household room)
```

---

## Build sequence (each step verified by execution in the worktree)

Worktree runs on a **throwaway port + DB** (`:8124`, `PLANTCART_DB=/tmp/…`) so it
never touches the live :8123 service or its real data.

**Phase A — multi-tenant backend (the foundation)**
1. `config.py` (env), `auth.py` (users, pbkdf2, JWT, dependency `current_household`).
2. Migrate schema: add users/households/members/catalog_snooze; add `household_id`
   to items + purchase_events; move revision per-household; retire catalog snooze col.
3. Scope every query in `db.py`/`catalog.py`/`app.py` by household_id; WS rooms;
   register/login/join/account-delete endpoints.
4. LLM adapter `llm.py`: provider = anthropic (Claude Haiku 4.5 default) |
   openai_compatible (vLLM) | none. Anthropic path uses `ANTHROPIC_API_KEY`.
5. Tests: two households can't see each other's lists; join-by-code shares a list;
   auth rejects bad tokens; account-delete purges. Verify end-to-end on :8124.

**Phase B — deployment (web service)**
6. `Dockerfile` (python:3.11-slim, uvicorn 0.0.0.0), `docker-compose.yml`
   (DB volume), `.env.example`, `fly.toml`. `DEPLOY.md`: Fly.io / Render / VPS,
   each terminating TLS. Verify: `docker build` succeeds; container serves /health.

**Phase C — mobile wrappers**
7. `capacitor.config.json` (bundled `app/` web assets, API base → hosted URL);
   Android build via GitHub Actions (x86 CI — reuse OutfitAdvisor precedent);
   `SUBMISSION.md`: Play + App Store runbooks, required assets, the user-only
   steps (developer accounts, signing, review).

**Phase D — web client auth**
8. Login / register / join-household screens gating the list; configurable API
   base; token on fetch + WS; account-delete + logout in a settings sheet.
   Verify: register → add items → second account joins by code → sees the list.

---

## What only the user can do (flagged, not blockers to the branch)

- **Hosting bill + domain**: pick Fly.io/Render/VPS, point a domain, set secrets.
- **Anthropic API key** (for hosted plants/recipes) — or leave LLM off.
- **Google Play**: Play Console account ($25 once), app signing, store listing.
- **Apple App Store**: Apple Developer Program ($99/yr), a Mac or cloud-Mac for
  the iOS build, App Review submission.
- **Legal**: privacy policy hosting (template provided), account-deletion URL.

---

## Explicitly OUT of this branch's scope (banked)

Postgres migration, payment/subscriptions, password reset email flow (needs an
email provider — documented stub), OAuth / Sign-in-with-Apple, push-notification
backend (FCM/APNs), household admin (kick/rename), horizontal scaling. All are
real follow-ups; the MVP proves the multi-tenant + tri-deploy shape first.

---

# Phase E — ship it: from verified branch to real web service (planned 2026-07-12, rev 15 — APPROVED by user 2026-07-13 with decisions: app name thincart, min_machines_running=1 confirmed, LLM launch-off but assignable, entitlement gating for recipes/advice, cutover today daytime)

Delegation: considered, rejected for the phase as a whole — cross-branch merge
with tenant-isolation judgment, live deploy, and real-data migration; not
mechanical. EXCEPTION: E0 step 3 (porting the regression-test inventory, pytest-gated)
fits the crew — re-run the delegation test there at
implementation time; per delegation.md the brief MUST embed grep-verified
schema DDL and the verbatim source tests — selected per step 1(d)'s
NEWEST-version rule (e.g. master's reworked test_plants.py, never
`3ddedb5`'s), since unverified or stale fixture claims are unfixable by
retries.

Goal: the hosted, public-HTTPS PlantCart becomes the **primary** instance the
two phones use daily; the DGX tailnet instance is retired after a soak. Web
service only — mobile store wrappers (Phase C) stay banked.

## Decisions to confirm at plan approval (user-owned)

| Decision | Recommendation | Why |
|---|---|---|
| Host | **Fly.io** | `fly.toml` + `deploy-fly.sh` already built; Render/VPS remain fallbacks in DEPLOY.md (which E1 updates — it still hardcodes `plantcart.fly.dev`) |
| App name | **`thincart`** (user-chosen 2026-07-13; Fly names are lowercase DNS labels, so "ThinCart" deploys as `thincart` → `https://thincart.fly.dev`; if taken, user picks a variant) (bare "plantcart" is almost certainly taken). `deploy-fly.sh` will `sed` the chosen name into fly.toml's `app =` line before deploying — `flyctl deploy -a <name>` against a mismatched fly.toml is an UNVERIFIED path (the script has never run past its auth check), so make the two agree instead of assuming the flag wins. All URLs/CORS derive from the real name; nothing hardcoded |
| Machines | **`min_machines_running = 1`** (change from current `0`); `auto_start_machines` stays `true` (harmless once the DB swap no longer stops the machine — see E4) | auto-stop would cold-start on nearly every phone unlock in-store; one always-on shared-cpu machine is ~$3–5/mo |
| LLM | launch with `PLANTCART_LLM_PROVIDER=none`; flip to `anthropic` + Haiku 4.5 key AFTER first deploy | the app must exist before `flyctl secrets set … -a <app>` can run, so key-then-deploy is impossible in one pass; flipping later is a config-only redeploy, token-safe per E1 step 8 |
| Registration | **closed after E3**, via a new flag `PLANTCART_REGISTRATION=open\|closed` where closed still allows **register-with-valid-invite-code** (plain closed would lock out the invited spouse — `/api/households/join` needs a bearer token, i.e. an existing account). Because the invite code then becomes an account-creation credential, E0 also adds **invite-code rotation** (`POST /api/households/rotate_invite`) — today's code is permanent, printed by the import script, and shown in settings; unrotatable = anyone who ever saw it can join forever | a public endpoint with open signup + LLM calls is a cost/abuse surface; rate limits throttle but don't cap spend |
| DGX instance | keep running untouched through the 1-week soak, then disable | rollback = point phones back at the tailnet URL — **but see Rollback: post-cutover data lives only on Fly** |

## E0 — branch reconciliation (code only, worktree :8124 + throwaway DB)

1. Merge **master HEAD at merge time** into `saas` — do NOT pin a SHA: the
   live app is under active development (master moved 8ccf697 → 370fcfb on
   2026-07-12 21:22, invalidating an earlier pinned analysis mid-review) —
   and follow the **merge-time protocol below, which is the ONLY
   authority** — any per-file statement elsewhere in this plan is a
   snapshot and is stale by definition (master gains features daily; it
   moved twice DURING this plan's review — 8ccf697→370fcfb→5566205 in one
   evening, the second adding a 419-line `server/plants.py`, a canonical
   plant-vocabulary rework of catalog.py, `tests/test_plant_vocab.py`, and
   turning `tests/test_plants.py` into a modify/delete conflict that an
   earlier revision of this plan wrongly called "untouched on master"):
   a. `git merge-tree --write-tree saas master` → the true CONFLICT set;
   b. `git log --oneline 3ddedb5..master` + `git diff --name-status
      3ddedb5..master` (the merge BASE, not any later analysis commit) →
      the full master-side feature/change inventory; write a one-line
      disposition for EVERY path — silent clean merges are where the traps
      live (unauthenticated UI features, new endpoints writing shared
      tables, new unauthenticated test files);
   c. conflict resolutions must preserve BOTH sides' semantic work: the
      saas side's multi-tenancy AND every master-side behavior since the
      base (as of 5566205 that means: history panel + `undo_purchase`,
      specificity/`_is_variety` guard, long-press editor + `edit` op,
      canonical plant vocabulary + AGP plant counting) — re-derive this
      list from step b's log at merge time, do not trust this snapshot;
   d. tests: for every test file existing on EITHER side of the merge,
      port the NEWEST version household-scoped — never a merge-base
      version when master carries a newer one (e.g. `test_plants.py` must
      come from master's current rework, not `3ddedb5`), and never drop a
      side's coverage silently: the step-7 pytest gate cannot fail on
      tests that were never ported, so a written per-file disposition is
      the only guard.
2. Port the history-panel feature to multi-tenant:
   - `GET /api/history` scoped by the caller's `household_id`;
   - `undo_purchase` must verify the target `purchase_events` row belongs to
     the caller's household (reject cross-tenant ids as no-op ACK);
   - the auto-merged history-panel UI in `app/index.html` ported to the
     authenticated client: history fetch carries the Authorization header,
     `undo_purchase` goes through the authed op path, `sw.js` cache bumped;
   - 370fcfb's long-press editor + `edit` op ported with **tenant
     isolation designed in, not bolted on**: qty edits target the
     household-scoped `items.qty_note` (safe as-is), but category edits
     must NOT write the SHARED `item_catalog.category` — one household's
     edit would silently re-categorize the item for every household.
     Instead: a per-household `catalog_category_override(household_id,
     catalog_id, category)` table (same pattern as `catalog_snooze`),
     read-preferred over the global value. **And the alias-merge path must
     learn about per-household reference tables**: on master, a category
     edit makes the catalog row unmergeable (edit-protects-from-merge
     invariant); the override table leaves the shared row's category NULL,
     so without changes the enrich sweeper would still merge it, DELETE the
     row, and orphan the override — silently reverting the household's
     edit. Spec: (a) rows referenced by ANY `catalog_category_override` are
     unmergeable (preserves master's invariant per-tenant); (b) alias-merge
     repoints `catalog_snooze` rows to the surviving catalog_id — audit the
     EXISTING saas merge code for this too: it currently repoints only
     items + purchase_events, so snoozes are already orphaned on merge
     today. The repoint must respect the composite
     `PRIMARY KEY(household_id, catalog_id)`: when a household snoozed BOTH
     the merged row and the survivor, a plain UPDATE violates the PK —
     coalesce on collision (keep the later `snoozed_until`, drop the source
     row); (c) `catalog_category_override` joins the household-delete purge
     list (db.py currently cascades only items/purchase_events/
     catalog_snooze/applied_ops) — otherwise a deleted household's
     overrides survive forever AND permanently pin their catalog rows
     unmergeable via rule (a). Tests: edit op scoped; household B's
     category view unchanged by household A's edit; merge blocked on
     overridden rows; snooze rows survive a merge repointed, including the
     both-rows-snoozed collision; account-delete leaves zero override rows;
   - the **3** history test functions from 8ccf697 (covering its 4 listed
     behaviors — verify the actual fold from `git show` at port time rather
     than trusting this note) rewritten household-scoped, plus one NEW test:
     household B cannot `undo_purchase` household A's event.
3. Delegation: considered, rejected — newest-version sources (795 lines
   across 4 files) + auth-fixture pattern + DDL exceed the crew's 24k-char
   context budget, and the same-day cutover favors a direct port with the
   pattern already in context.
   **Port the full regression suite per step 1's rule (d)** — every test
   file on either side, NEWEST version, household-scoped. **No fixed test
   COUNT belongs in this plan or in any crew brief** — counts drift with
   master (an earlier revision said "28" when the true newest-version
   inventory was already 55); the acceptance check is per-FILE: every test
   file named by `git diff --name-status 3ddedb5..master -- tests/` plus
   every file deleted on saas has a written ported-or-obsolete
   disposition, with per-test counts derived from the files at merge
   time. Snapshot as of 5566205 (re-derive!): test_ops.py, test_plants.py,
   test_catalog_candidates.py, test_skip_verify.py (deleted on saas; port
   master's newest versions), test_specificity.py, test_plant_vocab.py
   (new on master, unauthenticated). Invariants like
   checkoff-vs-remove race, add-idempotent-per-canonical-name, revision
   monotonicity, alias-merge-repoints-history, typo-suspect-hidden,
   LLM-down-returns-503 have NO household-scoped equivalents in
   `test_saas.py`, yet the merge touches exactly the files that implement
   them. All 28 get household-scoped ports (or a written note per test if one
   is genuinely obsolete under multi-tenancy — no silent drops).
   *(Crew-delegable under the brief constraints in the Delegation note.)*
4. `PLANTCART_REGISTRATION` flag: `open` (default) | `closed`. The
   `invite_code` register-body field is honored in **BOTH modes** (present
   + valid → account auto-joins that household; present + invalid → 403,
   never a silent solo household — an early-onboarding spouse must not end
   up in an empty list she thinks is shared). When closed, register without
   a code → 403. Tests: closed+no-code → 403; closed+valid → joined right
   household; closed+bad → 403; **open+valid → joined; open+bad → 403**.
   **The web auth screen is part of this port**: today's UI join flow
   (app/index.html ~line 978) calls `/api/auth/register` WITHOUT the code
   and only joins afterwards — in closed mode that register call 403s
   before join is ever reached, stranding the real browser onboarding
   path. The join-mode form must send `invite_code` in the register body,
   and step 18's live closed-mode check runs through THIS UI flow, not a
   hand-crafted curl. The register-path code lookup must reuse the join
   path's normalization contract (`invite_code.strip().upper()`,
   auth.py:94) and the UI must uppercase the field — an exact-match lookup
   passes every pasted-code test yet 403s the spouse's hand-typed
   lowercase code at cutover; add a lowercase-typed-code test.
5. `POST /api/households/rotate_invite` (bearer-auth, member-only) →
   regenerates `invite_code`; test: old code stops working for join AND for
   closed-mode register. Settings UI gets a "rotate" button next to the
   code. **Same change lengthens the code**: once registration closes, the
   invite code is the sole account-creation credential, and the current
   6-char/32-alphabet format is ~30 bits — guessable at even odds within
   months by a botnet staying under every per-IP limit. Generate ≥16 chars
   (~80 bits) for new/rotated codes; the spouse types it once. **Inventory
   every length cap built around the old format**: `JoinBody`'s
   `max_length=12` validator (server/app.py:101), the register-body
   validator, and the UI's `maxlength="12"` input (app/index.html:306) —
   any one of them 422s or silently truncates the new code, hard-blocking
   spouse onboarding after the no-flip-back close. The rotation test must
   assert the NEW long code SUCCEEDS for join and register, not just that
   the old one stops working.
5b. **Entitlement gating for paid features** (user decision 2026-07-13:
   recipes/advice = paid tier; list + plant count = free core). Minimal
   Phase E version: `households.tier TEXT DEFAULT 'basic'`; `/api/ideas`
   (recipes + diversity suggestions) returns 402/feature-hidden unless
   `tier='plus'`; the plant-count widget stays free (its data is cached
   enrichment, no per-request LLM cost). Tier is operator-set for now
   (direct DB update documented in DEPLOY.md) — payment processing stays
   BANKED; this just puts the seam where billing will later attach. UI
   hides the recipes button for basic tier. Tests: basic household gets
   402 on /api/ideas; plus household gets ideas; plant counter works for
   both.
6. Add a real auth **rate-limit test** (hammer register/login → 429). No such
   test exists today (only a fixture that resets the limiter); the limiter
   lives in `app.py` — a merge-conflict file — so it needs a pin before E1
   can honestly claim it survived the merge.
   **Plus the client-IP fix that makes the limiter meaningful in
   production**: the limiter keys on `request.client.host`, and behind
   fly-proxy every request arrives from the proxy's internal IP — ALL
   clients share ONE bucket, so any stranger sending 20 auth requests in
   5 min locks the household out of login. Do NOT fix this with
   `--proxy-headers --forwarded-allow-ips="*"`: Fly's proxy APPENDS to
   client-supplied `X-Forwarded-For` rather than stripping it, and
   trust-all mode takes the LEFTMOST (attacker-chosen) entry — that would
   make the limiter trivially spoofable (rotate a fake XFF per request →
   unlimited brute-force). Instead, key the limiter on the
   **`Fly-Client-IP` header** (set authoritatively by fly-proxy, the only
   header Fly documents as trustworthy) — **but ONLY when
   `PLANTCART_TRUST_FLY_CLIENT_IP=1`** (set in fly.toml's `[env]`,
   default OFF): on the non-Fly deploy paths DEPLOY.md keeps (Render, VPS,
   docker-compose, the DGX worktree) nothing strips a client-forged
   Fly-Client-IP, so unconditional trust would reintroduce the exact
   rotate-a-header bypass through the new header. Fallback in all other
   cases: `request.client.host`. Same change adds **eviction** to the
   limiter dict (drop keys whose newest hit is older than the window, on
   each call): per-real-IP keying on a public endpoint otherwise grows one
   permanent entry per scanner IP until the 512 MB machine OOMs. Pure
   app-code change — no uvicorn flags, no entrypoint coupling. **Flag on +
   header ABSENT falls back to the peer IP** (never a KeyError/500 — `fly
   proxy` debugging, .internal requests, and local repro with production
   env all reach uvicorn without fly-proxy headers). Unit tests: flag on →
   different `Fly-Client-IP` values get separate buckets and spoofed
   `X-Forwarded-For` never influences bucketing; flag on + no header →
   peer-IP bucket, no error; flag off → `Fly-Client-IP` is IGNORED (peer
   IP keys the bucket); idle keys evicted.
7. Full suite on :8124, output to `test_results/` per house rule; syntax-check
   all edited files; commit.

## E1 — pre-deploy hardening gate (still local)

8. **Fix `deploy-fly.sh`** (three edits, one commit):
   - `PLANTCART_SECRET`: set **only if absent** (`flyctl secrets list` check)
     — today it regenerates on every run, so any config redeploy (LLM flip,
     registration flip) would rotate the JWT secret and force-logout both
     phones mid-soak. Because this removes the only (accidental) rotation
     mechanism, the DEPLOY.md rewrite (step 10) must document DELIBERATE
     rotation as a runbook: `flyctl secrets set PLANTCART_SECRET=<new> -a
     <app>` — force-logs-out every session by design; this is the
     break-glass for a lost phone or leaked token;
   - set `PLANTCART_CORS=https://$APP.fly.dev` (derived, never hardcoded);
   - `sed` the app name into fly.toml's `app =` line so toml and `-a` agree.
9. **Dockerfile/entrypoint fixes** (the image as-is is dead on arrival on a
   Fly volume), then `docker build`:
   - add `server/backup_db.py` (stdlib `sqlite3` — no apt package needed)
     with a **required `--db <path>` argument** — the script has four call
     sites targeting three different databases (container live DB for the
     nightly backup and the step-21b swap checkpoint; a local dry-run copy
     in step 11; the local fresh import in step 20), and a config-default
     path resolution would let a local invocation silently checkpoint the
     WRONG database while exiting 0, stranding the imported rows in a
     `-wal` the upload omits. Default mode runs `Connection.backup()` to a
     **timestamped artifact** (`backup-YYYY-MM-DDTHHMMZ.db`, UTC to the
     minute — date-only stamps cannot support an hour-granularity
     freshness check — next to the DB,
     pruned on-machine) + `PRAGMA integrity_check`, exit non-zero on
     failure, and **prints `ARTIFACT:<filename>` on stdout as its last line**
     (the pinned protocol line step 23's fetch chain greps for — it must
     ship in the E1-built script, not be retrofitted onto the live primary
     mid-soak); a `--checkpoint` flag runs `PRAGMA wal_checkpoint(TRUNCATE)`
     and exits non-zero unless the result row reports `busy=0` (retrying a
     few times first). The step-23 nightly
     backup and the step-21b swap checkpoint both call this script — no
     quoted `sqlite3 '.backup …'` one-liners (`fly ssh console -C` does not
     pass through a remote shell; nested-quote handling is not guaranteed)
     and no extra CLI package for a capability python3 already has;
   - Fly mounts the `/data` volume **root-owned**, but the app runs as
     `USER plantcart` (uid 10001) → first boot cannot create
     `/data/plantcart.db` and crash-loops; likewise anything `fly sftp`/
     `fly ssh` places on the volume is root-owned. Fix: an `entrypoint.sh`
     that starts as root, `chown -R plantcart /data` when present, then
     drops privileges and execs uvicorn with the exact incantation
     `exec setpriv --reuid plantcart --regid plantcart --clear-groups
     uvicorn app:app --host 0.0.0.0 --port 8123` — the `--clear-groups` is
     load-bearing: setpriv REFUSES `--regid` without a group-handling flag
     (verified locally: exits 1, "requires --keep-groups, --clear-groups,
     …"). This also requires **removing the Dockerfile's `USER plantcart`
     directive** (line 32) — with it in place the entrypoint runs as uid
     10001 and both the chown and the setpriv fail. The boot-time chown
     also self-heals ownership of any root-uploaded file after the E4
     swap's restart.
   - Verify locally: run the container with a **root-owned** bind-mounted
     data dir (simulating the Fly volume) **and
     `-e PLANTCART_DB=/data/plantcart.db`** — without pinning the env var
     the app writes to the image's build-time-chowned `/srv/server/data`,
     the health check goes green, and the entrypoint chown/setpriv fix
     ships unexercised. Check health via
     `docker inspect --format='{{.State.Health.Status}}'` (not a localhost
     curl) **and assert PID 1 is non-root** (`docker exec <id> stat -c %u /proc/1`
     = 10001) — NOT `docker exec ... id -u`, which measures the exec
     session (root once the `USER` directive is dropped) and would fail on
     a correctly built image; dropping the `USER` directive demotes
     never-run-as-root from an image property to an entrypoint property,
     so it needs a check aimed at the actual server process; the user runs any browser/curl
     spot-check per env-constraints.
10. Checklist: no secrets in repo; `.env.example` covers every config key
    including `PLANTCART_REGISTRATION`; `fly.toml` gets
    `min_machines_running = 1`; **update DEPLOY.md** — its Fly section still
    hardcodes `plantcart.fly.dev` in the CORS secret + health-check lines and
    duplicates what deploy-fly.sh now does; rewrite it around the script and
    `<app>` placeholders (Render/VPS sections keep manual CORS but with
    placeholder domains). The rewrite must also document the
    `PLANTCART_TRUST_FLY_CLIENT_IP` flag: **Fly only** — enabling it behind
    Render/Caddy/nginx (which pass unknown headers through) would let a
    client-forged `Fly-Client-IP` bypass the auth rate limiter per-request.
11. Dry-run migration against a **fresh `sqlite3 .backup` copy of the live
    DB taken at E1 time** — the live DB is no longer empty: real family
    usage resumed hours after the 2026-07-11 reset (39 purchase events +
    207 catalog rows verified read-only on 2026-07-12 evening) and it only
    grows richer while E0 runs. Secondary fixture if ever needed:
    `~/backups/plantcart/keep-dryrun-plantcart-2026-07-11.db` (the 29-event
    pre-reset backup, already copied 2026-07-12 to a name the nightly
    keep-14 prune glob `plantcart-*.db` cannot sweep — the original dated
    copy gets pruned ~2026-07-25). Gate precondition either way: assert the
    fixture's `purchase_events` count is > 0 first — an events-empty
    fixture makes this gate vacuous. Throwaway
    credentials (not the user's real ones). Verify: full catalog imports,
    all events remap (counts equal source), cycles/suggested-tray output
    reproduces vs the same fixture under the old code (compared via
    pytest/TestClient in-process — no localhost curls). Import runs
    **locally into a fresh file**; then finalize it with
    `python3 server/backup_db.py --db <fresh-import-file> --checkpoint` +
    `PRAGMA integrity_check`
    — the SAME guarded helper as everywhere else, never a bare inline
    checkpoint (which exits 0 even when a lingering reader — e.g. the
    parity-verification process — blocks it, stranding the newest imported
    rows in a `-wal` the upload then omits). This same recipe is the E4
    production one (step 20).

## E2 — deploy (user actions interleaved; per env-constraints I don't touch .env, billing, or real credentials)

12. User: `flyctl auth login`, billing, pick the unique app name.
    (No manual app/volume/secret creation — `deploy-fly.sh` does all three.)
13. Run `./deploy-fly.sh <app>` (the name argument is REQUIRED — bare
    invocation exits 1). First deploy is LLM-off (`provider=none`).
14. Verify `fly status` + `curl -fsS https://<app>.fly.dev/health`
    (public URL — allowed; localhost curls are not).
15. LLM enablement (optional) is **deferred until AFTER step 18 closes
    registration** — enabling a billable key while the public endpoint
    still has open signup is a cost window (anyone can register and hammer
    the recipe/enrichment endpoints; the rate limiter throttles but does
    not cap spend). When the time comes: user runs
    `flyctl secrets set ANTHROPIC_API_KEY=… -a <app>`, sets
    `PLANTCART_LLM_PROVIDER = "anthropic"` in fly.toml, re-runs
    `./deploy-fly.sh <app>` — a config-only redeploy, token-safe after
    step 8, so nothing is lost by sequencing it late.

## E3 — live verification (execute the real path, not code review)

16. Register THREE throwaway accounts: two form the primary test household
    (join by invite code → two-browser live sync <2 s, WS over `wss://`,
    offline-queue replay, history panel + `undo_purchase`, cross-household
    isolation spot-check, invite rotation); the **third** exists solely to
    verify account-delete purges — delete IT, never the primary pair.
    **The primary throwaway household must survive through step 18**: its
    invite code is the input to the closed-mode register test, and once the
    flag flips there is no way to mint a code without an existing account
    (account-delete cascades solely-owned households — deleting the
    primaries first would make step 18 unexecutable without violating its
    own no-flip-back rule).
17. PWA install check on a real phone over the public HTTPS origin (secure
    context: SW caching + A2HS must both work — untestable publicly before).
18. **Close registration for good**: `flyctl secrets set
    PLANTCART_REGISTRATION=closed -a <app>` (a secrets set restarts the
    machine — fine pre-cutover). Then **live-verify the closed-mode invite
    path on the deployed app** — register one more throwaway account using
    the throwaway household's **current (post-step-16-rotation)** invite
    code — the code captured at registration is dead after the rotation
    test, and confirm no-code registration
    403s. This is the exact mechanism the spouse uses in step 22 on real
    data; it must not run for the first time mid-cutover (env-flag parsing
    of the secrets value, restart interplay, and proxied request bodies are
    all live-only surfaces the E0 unit tests can't touch). From here on,
    account creation requires an invite code; there is no later "flip
    back" — **and since password reset is deliberately banked, the
    documented break-glass for a forgotten password is operator access**:
    reset the `pw_hash` directly on the DB via `fly ssh console` (a
    python3 one-liner using `server/auth.py`'s hash function — add the
    exact command to DEPLOY.md's rewrite in E1 step 10). Register-again
    can't recover an account (duplicate email → 409). Acceptable for a
    two-user household; a real reset flow remains future work. E3's
    throwaway rows are harmless — E4 replaces the DB file wholesale.

## E4 — real-data migration + phone cutover + soak

19. **Phone queue drain BEFORE the freeze** — the write-freeze below is
    server-side only; the installed PWAs keep queueing offline ops and
    showing them as saved, and any op still queued against the tailnet
    origin is silently abandoned when the phones move to the fly.dev
    origin. So, with the DGX service still up: both phones open the app,
    confirm the **synced** pill (queue flushed), and the household agrees
    not to touch the app until step 22 completes (a same-evening window).
    Then freeze the source for real: `systemctl --user disable --now
    plantcart.service` — **disable, not just stop**: the unit is
    `WantedBy=default.target`, so a mere stop resurrects the "frozen"
    service on any DGX reboot or re-login during the soak, and a phone
    still carrying the old PWA would write to the stale DB. Re-enable only
    on rollback. **In the same sitting, disarm the two automation paths
    that would restart it anyway** (`systemctl restart` starts even a
    disabled unit): the `preflight-plantcart` skill auto-restarts
    plantcart.service unconditionally — and mid-soak glitches are exactly
    its documented trigger — and the `restartservice` skill covers
    plantcart too. Edit both NOW (not at step 24) to mark plantcart
    "FROZEN — cutover in progress, do NOT start; the Fly instance is
    primary"; step 24 does the full rewrite. Then
    take the final snapshot with the guarded helper (absolute path — the
    script lives on the saas branch, i.e. the WORKTREE, until step 24's
    merge) —
    `python3 ~/Project/plantcart-saas/server/backup_db.py --db ~/Project/plantcart/server/data/plantcart.db`
    (integrity-checked, not a bare `.backup`), then **`cp` the printed
    `backup-<stamp>.db` artifact** (the helper always writes next to the
    DB and has no --out flag; without this copy the documented rollback
    file never exists) — saved as
    `~/backups/plantcart/pre-migration-final.db` — a name that does NOT
    match the nightly prune glob `plantcart-*.db`, because the old backup
    timer keeps running through the soak and its keep-14 sweep would
    silently delete a conventionally-named "kept indefinitely" snapshot
    around day 13. The service stays stopped unless rolling back — any
    write after the snapshot would be silently lost.
20. **User** runs the import (it takes the real email/password as CLI args —
    plaintext credentials never pass through my shell or transcripts):
    `import_single_tenant.py` locally into a fresh SaaS DB file — **the
    import itself creates account #1 and the household** (no separate
    create-account step). Checkpoint + integrity-check per the step-11
    recipe. Treat the script's JSON output as sensitive (it prints the
    invite code). Verify counts + cycles parity locally vs the DGX snapshot.
21. **DB swap — the machine stays RUNNING throughout** (`fly sftp`/`fly ssh`
    need a started machine; stopping first is physically impossible to
    follow, and a stopped window would race `auto_start_machines`):
    a. `fly sftp` put the imported DB to `/data/plantcart.db.incoming`;
    b. `fly ssh console`: **checkpoint the old DB first** via
       `python3 /srv/server/backup_db.py --db /data/plantcart.db --checkpoint`
       (E1 step 9's script
       — it verifies the checkpoint result row reports `busy=0` and exits
       non-zero otherwise; a bare CLI checkpoint exits 0 even when a
       lingering reader — health check, WS holdover — blocks it, which
       would strand committed writes in the WAL the next command deletes).
       Retry until it exits clean. Then move the old DB aside
       (`mv /data/plantcart.db /data/plantcart.db.pre-migration`) **and
       delete its now-empty stale sidecars** (`rm -f /data/plantcart.db-wal
       /data/plantcart.db-shm` — the app never closes its connection, so a
       leftover WAL would otherwise be replayed INTO the new file on boot,
       silently corrupting it), then atomic
       `mv /data/plantcart.db.incoming /data/plantcart.db` (same volume;
       ownership self-heals via the entrypoint chown on the next restart);
    c. `fly machine restart` — the app's module-level connection holds the
       old inode until the process dies; restart forces a clean open of the
       new DB and fresh WAL state. The b→c window is idle by construction
       (registration closed, phones not yet cut over) — one stray request
       writing to the doomed inode is the accepted residual risk;
    d. verify — split by who holds credentials: I check `/health`, machine
       state, and logs; the **user** (whose real password step 20 keeps out
       of my shell and transcripts) logs in from a browser and confirms
       list + history counts + suggested tray, performs one write op, and
       then after ANOTHER `fly machine restart` confirms the write survived
       — proving persistence landed in the new file.
    Keep `/data/plantcart.db.pre-migration` on the volume as the
    instant-rollback copy **for the soak week only** (deleted in step 24 —
    the 1 GB volume shouldn't carry a stale twin forever); the DGX-side
    snapshot from step 19 is the copy kept indefinitely.
22. Spouse onboarding: she registers **with the household invite code**
    (closed-mode path, E0 step 4) — account created + joined in one step.
    Then **rotate the invite code** (E0 step 5). Both phones: A2HS on
    `https://<app>.fly.dev` (wife's iPhone: Safari A2HS — no Tailscale
    needed anymore, which removes the #1 adoption risk of the original
    plan) — **and DELETE the old tailnet PWA icon from both phones in the
    same sitting**: the old installed app keeps working from its
    service-worker cache against the dead origin, accepts writes into its
    offline queue, and shows them as saved — a muscle-memory tap in-store
    silently loses data for the whole soak and beyond.
23. **Off-Fly backups as a NEW standalone unit** `plantcart-fly-backup.timer`
    (nightly): `fly ssh console -a <app> -C "python3 /srv/server/backup_db.py
    --db /data/plantcart.db"` (the
    quote-free script from E1 step 9 — `-C` does not pass through a remote
    shell, so a nested-quote `sqlite3 '.backup …'` one-liner is not
    guaranteed to parse) then `fly sftp get` **today's date-stamped
    artifact** into `~/backups/plantcart-fly/` (keep 14). **One clock, one
    name authority**: `backup_db.py` stamps the artifact with the **UTC**
    date (the Fly container has no TZ set) and **prints the artifact
    filename to stdout on a pinned protocol line** (`ARTIFACT:<name>`,
    matched with an exact-prefix grep — the same untrusted `-C` channel
    carries the name, so an absent/garbled protocol line is itself treated
    as failure → alert, never a guess); the DGX side fetches exactly the
    printed name rather than computing "today" on its own clock — an independently
    computed ET (or even UTC) date diverges from the stamp whenever a
    `Persistent=true` catch-up run fires near UTC midnight (DGX asleep at
    03:30, wakes in the evening), which would fail the get and fire a
    false Telegram alarm nightly-fatigue-style. Timer `OnCalendar` 03:30
    America/New_York; freshness is judged from the fetched artifact itself
    (stamp within the last 24 h). Success is judged
    **on the DGX side from the artifact, never from remote exit codes**
    (flyctl's `-C` exit-code propagation is not trusted): the pulled file
    is the one whose name the remote script printed, must be non-empty,
    pass a local `PRAGMA integrity_check`, and carry a stamp within the
    last 24 h — never a locally-computed "today" comparison, which is the
    UTC-midnight race all over again. The stamp check is what makes a
    stale artifact a detectable failure, not a silent false success.
    **Prove the pipeline once before trusting it**: on day one of the soak,
    trigger the unit manually and watch the full chain pass — a backup path
    that has never produced a restorable artifact is not a backup path.
    **Failure must reach a human**: `OnFailure=` triggers a unit that sends
    a Telegram message via the existing ClaudeBridge bot (the user's
    already-running notification channel) — a journal line nobody reads is
    not an alert. But `OnFailure=` only fires when the unit RUNS and fails
    — it cannot catch the likelier death mode of the timer never firing
    (DGX asleep at 03:30, linger lost, unit disabled in a cleanup). Two
    guards: `Persistent=true` on the timer (missed ticks run on wake), and
    a **freshness watchdog outside the unit itself** — the
    `preflight-plantcart` skill asserts the newest
    `~/backups/plantcart-fly/` artifact is <48 h old. **This assertion is
    added in the SAME step-19 freeze-time skill edit that adds the FROZEN
    marker** (not deferred to step 24's rewrite) — otherwise the watchdog
    doesn't exist during the soak week, exactly when the pipeline is
    newest and likeliest to die. Gate: alarm when the newest artifact is >48 h old **or when the
    directory is still empty >48 h after the step-19 freeze marker** — a
    plain non-empty gate would stay mute forever if the pipeline never
    produces its first artifact (unit never enabled, day-one proof
    slipped), which is exactly the death mode a watchdog exists to catch;
    the 48 h grace covers the cutover window before the first backup. This unit is permanent post-soak
    infrastructure; it must fail loudly for years, not weeks. Deliberately
    NOT an extension of the old DGX timer, so retiring the DGX service later
    cannot take the hosted primary's backups down with it. This unit runs
    from day one of the soak and **permanently thereafter**.
24. 1-week soak, DGX instance disabled-but-intact as rollback. After soak:
    `systemctl --user disable --now plantcart-backup.timer` (the service
    was already disabled at step 19; `plantcart-fly-backup.timer` stays),
    turn off the `tailscale serve` proxy for :8123, delete
    `/data/plantcart.db.pre-migration` from the volume, keep all DGX-side
    backup history; merge `saas` → `master` so one branch is the deployed
    truth; repoint or retire the worktree. **Disposition BOTH frozen skills
    in the same sitting** (the step-19 markers are temporary, not an end
    state): rewrite `preflight-plantcart` around the Fly primary (health
    via public URL, fly-backup artifact freshness) dropping the DGX-service
    auto-restart, and remove or retarget the `restartservice` skill's
    plantcart entry — left merely frozen, it would claim "cutover in
    progress" about a retired service forever.

## Rollback — and what it costs

Before cutover (through step 18): zero risk — the DGX instance and its DB are
untouched; abandon the Fly app at any point.

After cutover (steps 19+): the DGX DB is frozen at the step-19 snapshot, so
**rolling back discards whatever was logged on Fly since cutover** unless
recovered. Rolling back means re-establishing the single-write-origin
invariant in REVERSE — as an ordered checklist, mirroring step 19:
1. Drain the phones against Fly while it is still up (both phones open the
   app, confirm the **synced** pill) — ops queued offline against the
   fly.dev origin are otherwise silently abandoned, the mirror of the
   forward drain.
2. Stop Fly writes with **`fly scale count 0 -a <app>`** — NOT `fly
   machine stop`: with `auto_start_machines = true` the proxy restarts a
   stopped machine on ANY incoming request (the plan already relies on
   this fact at step 21), so a stopped machine is not a write barrier; a
   machine that doesn't exist is.
3. Delete the fly.dev PWA icons from both phones; **re-A2HS the tailnet
   PWA** (step 22 deleted those icons — without this, no phone has any
   installed app after rollback).
4. Re-enable + start plantcart.service; **revert the step-19 FROZEN
   markers in both skills** (preflight-plantcart, restartservice) —
   otherwise the resurrected primary has no automation path willing to
   restart it on the next glitch.
5. **Stand down the step-23 pipeline**: disable plantcart-fly-backup.timer
   and remove the fly-artifact freshness watchdog — left running against
   a scaled-to-zero app, the timer fails (and alarms) nightly, and its
   `fly ssh console` has nothing to reach. Mitigations: `/data/plantcart.db.pre-migration` on the volume
covers instant same-day reversal of a botched swap; the nightly checkpointed
pulls (step 23) bound later loss to <24 h, and a pulled SaaS DB can be
inspected with sqlite3 to hand-copy the delta (deliberately no automated
reverse-migration — not worth building for a 1-week window). This trade is
accepted explicitly, not silently. The on-volume pre-migration copy lives for
the soak week (step 24 deletes it); the DGX-side step-19 snapshot is kept
indefinitely.
