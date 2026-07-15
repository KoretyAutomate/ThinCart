#!/usr/bin/env bash
# Turnkey Fly.io deploy for ThinCart. Run AFTER `flyctl auth login`.
#
#   ./deploy-fly.sh <unique-app-name>
#
# Idempotent-ish: re-running redeploys. It creates the app + a persistent volume,
# sets a strong JWT secret (generated here, never committed), and deploys via
# Fly's remote builder (so this aarch64 box doesn't need to build the image).
set -euo pipefail

APP="${1:-}"
REGION="${FLY_REGION:-nrt}"   # Tokyo
if [[ -z "$APP" ]]; then
  echo "usage: ./deploy-fly.sh <unique-app-name>   (e.g. thincart-arai)" >&2
  exit 1
fi

export PATH="$HOME/.fly/bin:$PATH"
command -v flyctl >/dev/null || { echo "flyctl not found; run the fly.io install first" >&2; exit 1; }
flyctl auth whoami >/dev/null 2>&1 || { echo "Not logged in. Run: flyctl auth login" >&2; exit 1; }

echo "==> Ensuring app '$APP' exists"
flyctl apps create "$APP" 2>/dev/null || echo "   (app already exists — reusing)"

# fly.toml's `app` must AGREE with -a: flyctl errors on a mismatch, and the
# shipped value is a CHANGEME placeholder. sed it in (idempotent).
sed -i "s/^app = .*/app = \"$APP\"/" fly.toml
echo "==> fly.toml app pinned to '$APP'"

echo "==> Ensuring persistent volume 'thincart_data' in $REGION"
if ! flyctl volumes list -a "$APP" 2>/dev/null | grep -q thincart_data; then
  flyctl volumes create thincart_data --size 1 --region "$REGION" -a "$APP" --yes
else
  echo "   (volume exists — reusing)"
fi

# Set the JWT secret ONLY IF ABSENT: regenerating on every run would rotate it
# on any config redeploy (LLM flip, registration flip) and force-logout every
# session mid-use. Deliberate rotation (lost phone, leaked token) is a manual
# runbook step — see DEPLOY.md.
if flyctl secrets list -a "$APP" 2>/dev/null | grep -q THINCART_SECRET; then
  echo "==> THINCART_SECRET already set — keeping it (rotation is manual; see DEPLOY.md)"
else
  echo "==> Setting THINCART_SECRET (generated, not stored anywhere else)"
  SECRET="$(python3 -c 'import secrets; print(secrets.token_urlsafe(48))')"
  flyctl secrets set "THINCART_SECRET=$SECRET" -a "$APP" >/dev/null
  echo "   secret set."
fi

# CORS derives from the app name — never hardcoded (a mismatched origin makes
# every browser API call fail after deploy).
flyctl secrets set "THINCART_CORS=https://$APP.fly.dev" -a "$APP" --stage >/dev/null
echo "==> THINCART_CORS=https://$APP.fly.dev (staged; applies with this deploy)"

echo "==> Deploying (remote builder)"
flyctl deploy -a "$APP" --remote-only --ha=false --yes

URL="https://$APP.fly.dev"
echo
echo "==> Deployed. Verifying /health ..."
sleep 5
curl -fsS "$URL/health" && echo
echo
echo "ThinCart is live at:  $URL"
echo "Open it, create an account, and share the invite code with your wife."
echo
echo "To close registration after the household is set up (invite-code-only from then on):"
echo "  flyctl secrets set THINCART_REGISTRATION=closed -a $APP"
echo
echo "To enable recipes / plant enrichment later (ONLY after registration is closed):"
echo "  1) flyctl secrets set ANTHROPIC_API_KEY=sk-ant-... -a $APP"
echo "  2) set THINCART_LLM_PROVIDER = \"anthropic\" in fly.toml, then: ./deploy-fly.sh $APP"
echo "     (config-only redeploys are token-safe: the JWT secret is never regenerated)"
