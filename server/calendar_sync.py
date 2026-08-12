"""
calendar_sync.py — read-only Google Calendar access (the only I/O in layer 1b).

PLAN.md §Intelligence layer 1b. Talks to the REST API with httpx directly
rather than google-api-python-client: a refresh-token exchange and one paged
GET is the whole surface, and ThinCart's dependency list is three packages.

Credentials live OUTSIDE the repo, in ~/.config/thincart/google_oauth.json —
ThinCart is a public repo and a refresh token is a bearer credential:

    {"client_id": "...apps.googleusercontent.com",
     "client_secret": "...",
     "refresh_token": "...",          # written by --authorize
     "calendar_ids": ["primary"]}

One-time setup, from the DGX:

    1. Google Cloud console → new project → enable the Google Calendar API
    2. OAuth consent screen → External → add yourself as a test user
    3. Credentials → OAuth client ID → **Desktop app** → note id + secret
    4. write those two into the file above, then:
       python3 calendar_sync.py --authorize

Sync failure is never fatal to ThinCart: the caller logs it and keeps the away
days it already has. A shopping list that cannot reach Google is still a
shopping list.
"""

import base64
import hashlib
import json
import os
import secrets
import threading
import urllib.parse
from datetime import datetime, timedelta, UTC
from pathlib import Path

import httpx

CREDS_PATH = Path(
    os.environ.get("THINCART_GOOGLE_OAUTH", Path.home() / ".config" / "thincart" / "google_oauth.json")
)

AUTH_URI = "https://accounts.google.com/o/oauth2/v2/auth"
# named for what it is — an endpoint URL. Calling it TOKEN_* tripped the
# hardcoded-credential lint, and silencing that would have spent a real
# suppression on a naming accident.
EXCHANGE_URI = "https://oauth2.googleapis.com/token"
API_ROOT = "https://www.googleapis.com/calendar/v3"
SCOPE = "https://www.googleapis.com/auth/calendar.readonly"

# How much history a sync looks at. Purchase cycles are estimated from a few
# months of events at most, so re-reading a year every 6 h would be waste.
WINDOW_BACK_DAYS = 180
WINDOW_AHEAD_DAYS = 30

TIMEOUT = httpx.Timeout(20.0)


class CalendarError(RuntimeError):
    """Anything that stopped a sync — missing credentials, or Google saying no."""


def load_credentials(path: Path = CREDS_PATH) -> dict:
    if not path.exists():
        raise CalendarError(f"no Google credentials at {path} — run calendar_sync.py --authorize")
    creds = json.loads(path.read_text())
    missing = [k for k in ("client_id", "client_secret") if not creds.get(k)]
    if missing:
        raise CalendarError(f"{path} is missing {', '.join(missing)}")
    return creds


def _save_credentials(creds: dict, path: Path = CREDS_PATH) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(creds, indent=2) + "\n")
    path.chmod(0o600)  # a refresh token is a password with no expiry


def is_linked(path: Path = CREDS_PATH) -> bool:
    """Whether a calendar is connected — drives the UI's 'not linked yet' state."""
    try:
        return bool(load_credentials(path).get("refresh_token"))
    except (CalendarError, json.JSONDecodeError):
        return False


def access_token(creds: dict) -> str:
    """Trade the long-lived refresh token for a ~1 h access token.

    Not cached: a sync runs every 6 hours, by which time any cached token has
    expired anyway, and a token held in memory is one more thing to leak.
    """
    if not creds.get("refresh_token"):
        raise CalendarError("credentials have no refresh_token — run calendar_sync.py --authorize")
    r = httpx.post(
        EXCHANGE_URI,
        data={
            "client_id": creds["client_id"],
            "client_secret": creds["client_secret"],
            "refresh_token": creds["refresh_token"],
            "grant_type": "refresh_token",
        },
        timeout=TIMEOUT,
    )
    if r.status_code != 200:
        # invalid_grant means the token was revoked or expired through disuse —
        # say so plainly, because the fix is re-authorizing, not retrying.
        raise CalendarError(f"token refresh failed ({r.status_code}): {r.text[:200]}")
    return r.json()["access_token"]


def fetch_events(
    creds: dict | None = None,
    time_min: datetime | None = None,
    time_max: datetime | None = None,
) -> list[dict]:
    """Every event in the window, across the configured calendars.

    singleEvents=true expands recurring events into their instances — a weekly
    recurrence would otherwise arrive as one master event whose dates say
    nothing about which days were actually occupied.
    """
    creds = creds or load_credentials()
    now = datetime.now(UTC)
    time_min = time_min or now - timedelta(days=WINDOW_BACK_DAYS)
    time_max = time_max or now + timedelta(days=WINDOW_AHEAD_DAYS)
    token = access_token(creds)

    events: list[dict] = []
    for calendar_id in creds.get("calendar_ids") or ["primary"]:
        url = f"{API_ROOT}/calendars/{urllib.parse.quote(calendar_id, safe='')}/events"
        params = {
            "singleEvents": "true",
            "orderBy": "startTime",
            "maxResults": 2500,
            "timeMin": time_min.isoformat().replace("+00:00", "Z"),
            "timeMax": time_max.isoformat().replace("+00:00", "Z"),
        }
        with httpx.Client(timeout=TIMEOUT, headers={"Authorization": f"Bearer {token}"}) as client:
            while True:
                r = client.get(url, params=params)
                if r.status_code != 200:
                    raise CalendarError(f"events.list {calendar_id} failed ({r.status_code}): {r.text[:200]}")
                page = r.json()
                events.extend(page.get("items", []))
                token_next = page.get("nextPageToken")
                if not token_next:
                    break
                params["pageToken"] = token_next
    return events


def list_calendars(creds: dict | None = None) -> list[dict]:
    """Calendar ids and names — for choosing what `calendar_ids` should hold."""
    creds = creds or load_credentials()
    r = httpx.get(
        f"{API_ROOT}/users/me/calendarList",
        headers={"Authorization": f"Bearer {access_token(creds)}"},
        timeout=TIMEOUT,
    )
    if r.status_code != 200:
        raise CalendarError(f"calendarList failed ({r.status_code}): {r.text[:200]}")
    return [{"id": c["id"], "summary": c.get("summary", "")} for c in r.json().get("items", [])]


# ---------------------------------------------------------------- authorize --


def _pkce_pair() -> tuple[str, str]:
    verifier = base64.urlsafe_b64encode(secrets.token_bytes(48)).rstrip(b"=").decode()
    digest = hashlib.sha256(verifier.encode()).digest()
    return verifier, base64.urlsafe_b64encode(digest).rstrip(b"=").decode()


def _serve_one_code(port: int, holder: dict, done: threading.Event) -> None:
    """Catch Google's redirect, if a browser on this machine can reach it."""
    from http.server import BaseHTTPRequestHandler, HTTPServer

    class Handler(BaseHTTPRequestHandler):
        # do_GET is http.server's required spelling, not a style choice
        def do_GET(self):
            query = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            holder.setdefault("code", (query.get("code") or [""])[0])
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            ok = bool(holder.get("code"))
            self.wfile.write(
                b"<h2>ThinCart is linked. You can close this tab.</h2>"
                if ok
                else b"<h2>No code in that redirect.</h2>"
            )
            done.set()

        def log_message(self, *args):
            pass

    server = HTTPServer(("127.0.0.1", port), Handler)
    server.timeout = 300
    while not done.is_set():
        server.handle_request()
    server.server_close()


def _extract_code(text: str) -> str:
    """Accept either a bare code or the whole redirect URL pasted from a browser."""
    text = text.strip()
    if text.startswith("http"):
        return (urllib.parse.parse_qs(urllib.parse.urlparse(text).query).get("code") or [""])[0]
    return text


def authorize(port: int = 8765, path: Path = CREDS_PATH) -> None:
    """One-time consent, storing the refresh token.

    Two ways in, because the DGX is headless and the browser is usually on
    another machine: the loopback listener handles the case where they are the
    same machine, and a pasted redirect URL handles the case where they are
    not. Whichever arrives first wins.
    """
    creds = load_credentials(path)
    verifier, challenge = _pkce_pair()
    redirect_uri = f"http://127.0.0.1:{port}"
    params = {
        "client_id": creds["client_id"],
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": SCOPE,
        "access_type": "offline",       # without this there is no refresh token
        "prompt": "consent",            # force one even if this app was approved before
        "code_challenge": challenge,
        "code_challenge_method": "S256",
    }
    print("\n1. Open this URL in any browser, signed in as the calendar's owner:\n")
    print(f"   {AUTH_URI}?{urllib.parse.urlencode(params)}\n")
    print(f"2. It redirects to {redirect_uri}/?code=…")
    print("   On this machine that completes by itself. From another machine the")
    print("   page will fail to load — copy its URL from the address bar and paste it here.\n")

    holder: dict = {}
    done = threading.Event()

    def read_pasted() -> None:
        holder.setdefault("code", _extract_code(input("Paste the redirect URL or code: ")))
        done.set()

    threading.Thread(target=_serve_one_code, args=(port, holder, done), daemon=True).start()
    threading.Thread(target=read_pasted, daemon=True).start()
    done.wait(timeout=600)

    code = holder.get("code")
    if not code:
        raise CalendarError("no authorization code received")

    r = httpx.post(
        EXCHANGE_URI,
        data={
            "client_id": creds["client_id"],
            "client_secret": creds["client_secret"],
            "code": code,
            "code_verifier": verifier,
            "grant_type": "authorization_code",
            "redirect_uri": redirect_uri,
        },
        timeout=TIMEOUT,
    )
    if r.status_code != 200:
        raise CalendarError(f"code exchange failed ({r.status_code}): {r.text[:300]}")
    refresh = r.json().get("refresh_token")
    if not refresh:
        raise CalendarError("Google returned no refresh_token; retry with prompt=consent")

    creds["refresh_token"] = refresh
    creds.setdefault("calendar_ids", ["primary"])
    _save_credentials(creds, path)
    print(f"\n✅ linked — refresh token saved to {path} (0600)")
    for cal in list_calendars(creds):
        print(f"   {cal['id']}  —  {cal['summary']}")
    print("\nEdit calendar_ids in that file if you want more than the primary calendar.")


if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser(description="ThinCart ↔ Google Calendar link")
    ap.add_argument("--authorize", action="store_true", help="run the one-time consent flow")
    ap.add_argument("--calendars", action="store_true", help="list calendars this link can read")
    ap.add_argument("--check", action="store_true", help="fetch the window and show what reads as travel")
    ap.add_argument("--port", type=int, default=8765)
    args = ap.parse_args()

    if args.authorize:
        authorize(args.port)
    elif args.calendars:
        for cal in list_calendars():
            print(f"{cal['id']}\t{cal['summary']}")
    elif args.check:
        import travel

        found = travel.detect(fetch_events())
        for trip in travel.group_trips(found):
            span = f"{trip['start']} → {trip['end']}" if trip["start"] != trip["end"] else str(trip["start"])
            print(f"{span}  {len(trip['days'])}d  {trip['summary']}  [{trip['reason']}]  {trip['location']}")
        print(f"\n{len(found)} away day(s) detected")
    else:
        ap.print_help()
