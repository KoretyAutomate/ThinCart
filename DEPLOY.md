# Deploying ThinCart (service name: thincart)

ThinCart is a single FastAPI process serving the API and the PWA, backed by one
SQLite file. All config is via environment variables (see `.env.example` /
`server/config.py`). The schema auto-creates (and additively migrates) on boot.

> **TLS is mandatory.** The web client is a PWA: service workers and secure auth
> only work over HTTPS. Every path below terminates TLS.

> **Scaling ceiling.** SQLite has a *single writer*. Run **one instance**. Do not
> scale horizontally — migrate to Postgres *before* adding instances.

> **App name.** Fly app names are global, lowercase DNS labels. This deployment
> uses **`thincart`** → `https://thincart.fly.dev`. Substitute your own unique
> name everywhere `<app>` appears.

---

## (a) Fly.io — the supported path

Everything is driven by `./deploy-fly.sh <app>` (the name argument is REQUIRED):

```bash
flyctl auth login          # once
./deploy-fly.sh thincart   # creates app + volume, pins fly.toml, sets secrets, deploys
```

The script is safe to re-run for config redeploys. What it does:

- `flyctl apps create` / volume create — idempotent, reused when present.
- Pins `app = "<app>"` into fly.toml (flyctl errors on a name mismatch).
- Sets `THINCART_SECRET` **only if absent** — re-runs never rotate the JWT
  secret, so config redeploys don't log the household out.
- Sets `THINCART_CORS=https://<app>.fly.dev` — always derived, never typed.
- Deploys via the remote builder and curls `/health`.

`fly.toml` ships production defaults: `min_machines_running = 1` (no cold-start
lag on phone unlocks), LLM off, and `THINCART_TRUST_FLY_CLIENT_IP = "1"`
(**Fly only** — see the warning below).

### Post-launch runbooks (all against the live app)

**Close registration** (after the household is onboarded; invite-code register
still works — it's the only account-creation path from then on):

```bash
flyctl secrets set THINCART_REGISTRATION=closed -a <app>
```

**Enable the LLM (recipes / enrichment)** — only AFTER registration is closed
(open signup + billable LLM = uncapped spend):

```bash
flyctl secrets set ANTHROPIC_API_KEY=sk-ant-... -a <app>
# then set THINCART_LLM_PROVIDER = "anthropic" in fly.toml and:
./deploy-fly.sh <app>
```

**Grant the paid tier** (recipes/advice; list + plant count are always free).
Operator-set for now — this is the seam billing will later attach to:

```bash
fly ssh console -a <app> -C "python3 -c \"import sqlite3;c=sqlite3.connect('/data/thincart.db');c.execute('UPDATE households SET tier=\\\"plus\\\" WHERE invite_code=\\\"<CODE>\\\"');c.commit()\""
```

**Rotate the JWT secret** (lost phone, leaked token). Deliberate and manual —
force-logs-out every session by design:

```bash
flyctl secrets set THINCART_SECRET="$(python3 -c 'import secrets;print(secrets.token_urlsafe(48))')" -a <app>
```

**Break-glass password reset** (there is no email reset flow; registering again
409s on a duplicate email). Reset the hash directly:

```bash
fly ssh console -a <app> -C "python3 -c \"
import sqlite3,sys; sys.path.insert(0,'/srv/server'); import auth
c=sqlite3.connect('/data/thincart.db')
c.execute('UPDATE users SET pw_hash=? WHERE email=?', (auth.hash_password('NEW-PASSWORD'),'user@example.com'))
c.commit()\""
```

**Rotate the invite code** — in the app: Settings → "New invite code". The old
code stops working for join AND for closed-mode register immediately.

**Nightly off-Fly backup** (run from any trusted box; see the systemd units in
`server/deploy/`): call `python3 /srv/server/backup_db.py --db /data/thincart.db`
over `fly ssh console -a <app> -C …`, grep stdout for the final `ARTIFACT:<name>`
line, `fly sftp get` exactly that file, then verify locally: non-empty, `PRAGMA
integrity_check` = ok, stamp within 24 h. Judge success from the artifact only —
never from remote exit codes.

---

## (b) Any VPS with Docker Compose + Caddy (fallback)

> ⚠️ **Never set `THINCART_TRUST_FLY_CLIENT_IP` here.** Caddy/nginx pass a
> client-forged `Fly-Client-IP` header straight through — trusting it lets an
> attacker mint a fresh rate-limit bucket per request (unlimited brute force).

```bash
cp .env.example .env
# edit .env: real THINCART_SECRET, THINCART_CORS=https://your.domain
docker compose up -d
```

Minimal `Caddyfile` (automatic Let's Encrypt):

```caddyfile
your.domain {
    reverse_proxy 127.0.0.1:8123
}
```

Update: `git pull && docker compose up -d --build`.

---

## (c) Render.com (fallback)

Same warning as (b): leave `THINCART_TRUST_FLY_CLIENT_IP` unset.

1. New **Web Service** → connect repo → Runtime: Docker. **Instance count: 1.**
2. Disk: mount `/data`, 1 GB.
3. Env vars: `THINCART_ENV=production`, `THINCART_DB=/data/thincart.db`,
   `THINCART_SECRET` (secret), `THINCART_CORS=https://<service>.onrender.com`,
   `THINCART_LLM_PROVIDER=none` (flip later with the key, as secrets).
4. Health check path: `/health`.
