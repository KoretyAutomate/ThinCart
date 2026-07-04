# Deploying PlantCart

PlantCart is a single FastAPI process serving both the API and the PWA, backed by one
SQLite file. All config is via environment variables (see `.env.example` / `server/config.py`).
The DB schema auto-creates on first boot.

> **TLS is mandatory.** The web client is a PWA: service workers and secure auth cookies
> only work over HTTPS. Never expose the app over plain HTTP beyond localhost. Every path
> below terminates TLS (Fly/Render do it for you; on a VPS use Caddy or nginx).

> **Scaling ceiling.** SQLite has a *single writer*. This app is designed to run as **one
> instance**. Do not scale horizontally — concurrent writers will corrupt or lock the DB.
> When you outgrow one machine, migrate to Postgres *before* adding instances.

Generate the JWT secret once (used in every path below):

```bash
python -c "import secrets;print(secrets.token_urlsafe(48))"
```

---

## (a) Fly.io

`fly.toml` is included; it sets `PLANTCART_ENV=production`, `PLANTCART_DB=/data/plantcart.db`,
and mounts a `plantcart_data` volume there.

```bash
# 1. First time: create the app from fly.toml (skip if it already exists).
fly launch --no-deploy --copy-config --name plantcart

# 2. Create the persistent volume the mount expects (same region as the app).
fly volumes create plantcart_data --size 1 --region iad

# 3. Set secrets (never put these in fly.toml).
fly secrets set PLANTCART_SECRET="$(python -c 'import secrets;print(secrets.token_urlsafe(48))')"
fly secrets set ANTHROPIC_API_KEY="sk-ant-..."

# 4. (optional) pin CORS to your web origin
fly secrets set PLANTCART_CORS="https://plantcart.fly.dev"

# 5. Deploy.
fly deploy

# 6. Verify.
fly status
curl -fsS https://plantcart.fly.dev/health    # -> {"ok":true,...}
```

Fly terminates TLS and `force_https = true` redirects HTTP→HTTPS. Machines auto-stop when
idle and auto-start on request (`min_machines_running = 0`); keep it at a single machine.

---

## (b) Any VPS with Docker Compose + Caddy (TLS)

```bash
# 1. Copy the repo to the server, then create .env from the template.
cp .env.example .env

# 2. Put a real secret + API key into .env.
sed -i "s|^PLANTCART_SECRET=.*|PLANTCART_SECRET=$(python -c 'import secrets;print(secrets.token_urlsafe(48))')|" .env
#   then edit .env: set ANTHROPIC_API_KEY and PLANTCART_CORS=https://your.domain

# 3. Bring it up (SQLite persists in the named volume `plantcart-data`).
docker compose up -d
docker compose logs -f plantcart          # watch for startup

# 4. Verify locally (before TLS is in front).
curl -fsS http://127.0.0.1:8123/health    # -> {"ok":true,...}
```

Put Caddy in front for automatic Let's Encrypt TLS (simplest option). Minimal `Caddyfile`:

```caddyfile
plantcart.example.com {
    reverse_proxy 127.0.0.1:8123
}
```

Run Caddy (`caddy run --config ./Caddyfile`, or as a systemd service / its own container).
It obtains and renews certificates automatically. Point DNS `A`/`AAAA` at the VPS first.

> nginx alternative: `proxy_pass http://127.0.0.1:8123;` inside a `server {}` block, with
> certs from certbot. Caddy is recommended because auto-TLS needs zero extra steps.

To update: `git pull && docker compose up -d --build`.

---

## (c) Render.com

1. New **Web Service** → connect the repo → **Runtime: Docker** (Render uses the root `Dockerfile`).
2. **Instance count: 1** (single SQLite writer — do not increase).
3. Add a **Disk**: mount path `/data`, size 1 GB. This holds the SQLite DB.
4. Environment variables:

   | Key | Value |
   |-----|-------|
   | `PLANTCART_ENV` | `production` |
   | `PLANTCART_DB` | `/data/plantcart.db` |
   | `PLANTCART_SECRET` | *(generated secret — mark as secret)* |
   | `PLANTCART_LLM_PROVIDER` | `anthropic` |
   | `ANTHROPIC_API_KEY` | `sk-ant-...` *(secret)* |
   | `PLANTCART_LLM_MODEL` | `claude-haiku-4-5-20251001` |
   | `PLANTCART_CORS` | `https://your-service.onrender.com` |

5. **Health Check Path:** `/health`.
6. Deploy. Render provides HTTPS on the `*.onrender.com` domain automatically.

Render sets `$PORT`; the app already listens on 8123 and Render maps to it via the
exposed port. If you prefer, set `PLANTCART_PORT` to match Render's `$PORT` — but the
default 8123 works with the Dockerfile's `EXPOSE`.
