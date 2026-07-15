"""
config.py — all runtime configuration via environment (12-factor).

Single-tenant master hardcoded the tailnet IP + local vLLM; the SaaS build reads
everything from env so the same image runs on a laptop, a VPS, or Fly.io.
"""
import os
from pathlib import Path


def _bool(v: str) -> bool:
    return str(v).lower() in ("1", "true", "yes", "on")


# --- server ---
ENV = os.environ.get("THINCART_ENV", "dev").lower()   # "production" enforces secrets
HOST = os.environ.get("THINCART_HOST", "0.0.0.0")
PORT = int(os.environ.get("THINCART_PORT", "8123"))
DB_PATH = Path(os.environ.get("THINCART_DB", Path(__file__).parent / "data" / "thincart.db"))

# --- auth ---
# MUST be overridden in production. A random default keeps dev safe but logs a warning.
SECRET = os.environ.get("THINCART_SECRET", "")
TOKEN_TTL_DAYS = int(os.environ.get("THINCART_TOKEN_TTL_DAYS", "30"))

# CORS: comma-separated origins, or "*" for dev. Prod should pin the web origin.
CORS_ORIGINS = [o.strip() for o in os.environ.get("THINCART_CORS", "*").split(",") if o.strip()]

# Registration: "open" | "closed". Closed still allows register-with-valid-invite-code
# (the ONLY way a spouse can be onboarded after the household is set up — /join
# needs an existing account's bearer token).
REGISTRATION = os.environ.get("THINCART_REGISTRATION", "open").lower()

# Trust the Fly-Client-IP header for auth rate limiting. Fly ONLY (fly-proxy sets
# it authoritatively); behind any other proxy the header is client-forgeable and
# this flag must stay off.
TRUST_FLY_CLIENT_IP = _bool(os.environ.get("THINCART_TRUST_FLY_CLIENT_IP", "0"))

# --- LLM provider: "anthropic" | "openai_compatible" | "none" ---
LLM_PROVIDER = os.environ.get("THINCART_LLM_PROVIDER", "none").lower()
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
# Cheap, fast model for high-volume enrichment; recipes can use the same or bigger.
LLM_MODEL = os.environ.get("THINCART_LLM_MODEL", "claude-haiku-4-5-20251001")
# openai_compatible (e.g. a self-hosted vLLM) settings
OPENAI_BASE_URL = os.environ.get("THINCART_OPENAI_BASE_URL", "http://127.0.0.1:8000/v1")
OPENAI_MODEL = os.environ.get("THINCART_OPENAI_MODEL", "Intel/Qwen3.5-122B-A10B-int4-AutoRound")

# SearXNG for new-item typo verification (optional; disabled if empty)
SEARX_URL = os.environ.get("THINCART_SEARX_URL", "")


def effective_secret() -> str:
    """Return the JWT signing secret. Hard-fails in production if unset (an unset
    secret would make every token forgeable); dev/test gets a warned fallback."""
    if SECRET:
        return SECRET
    if ENV == "production":
        raise RuntimeError(
            "THINCART_SECRET must be set in production (THINCART_ENV=production). "
            "Generate one: python -c 'import secrets; print(secrets.token_urlsafe(48))'"
        )
    import logging
    logging.getLogger("thincart.config").warning(
        "THINCART_SECRET unset — using an insecure dev fallback. SET THIS IN PRODUCTION."
    )
    return "dev-insecure-secret-change-me"
