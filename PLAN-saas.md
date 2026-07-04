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
