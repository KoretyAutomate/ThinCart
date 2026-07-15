# ThinCart SaaS — FastAPI + SQLite, single-image deploy.
# Layout inside the image mirrors the repo so app.py's `Path(__file__).parent.parent / "app"`
# resolves: code lives at /srv/server, the PWA at /srv/app, WORKDIR is /srv/server.
FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

# curl is only needed for the container HEALTHCHECK below.
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*

# Non-root runtime user.
RUN useradd --create-home --uid 10001 thincart

WORKDIR /srv/server

# Install deps first for layer caching.
COPY server/requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

# App code + PWA static assets (../app must resolve from server/).
COPY server/ /srv/server/
COPY app/ /srv/app/
COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

# Default DB location; override with THINCART_DB to point at a mounted volume.
# The dir is owned by the runtime user so SQLite can create the file on boot.
RUN mkdir -p /srv/server/data && chown -R thincart:thincart /srv

# NO `USER` directive: the entrypoint must start as root to chown the Fly
# volume mount (/data is root-owned on first boot, and sftp'd files are too),
# then it drops to uid 10001 via setpriv before exec'ing uvicorn. The
# never-run-as-root invariant is therefore an ENTRYPOINT property — pinned by
# the E1 verification (`stat -c %u /proc/1` inside the running container).

EXPOSE 8123

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD curl -fsS http://127.0.0.1:8123/health || exit 1

ENTRYPOINT ["/entrypoint.sh"]
