# ThinCart 🌱

Self-hosted shared shopping list for two phones with real-time sync, plus:
purchase-cycle recommendations ("you're due for milk") and plant-diversity
tracking toward **30 different plants a week**, powered by the local DGX LLM.
See `PLAN.md` for the full architecture (agent-reviewed, approved 2026-07-03).

## Status

- **Phase 0 — real-time sync**: ✅ live (idempotent ops, WS broadcast, offline queue).
- **Phase 1 — cycle recommendations**: ✅ live. Every item ever bought is in
  scope, grouped by whole weeks (weekly, bi-weekly, every 3 weeks, … no top
  bin) and split into **Buy now** (due, on a rhythm worth trusting) vs **Might
  need this week** (due but the history is thin or erratic, or arriving within
  the week's remaining at-home days). Measured from the last 4 intervals, so a
  rhythm that changed stops arguing with the current one.
- **Phase 2 — plants + recipes**: ✅ live (LLM enrichment, 🌱 counter, ideas panel).
- **Candidates + categories**: ✅ live — 173-item seeded catalog, typing dropdown
  (kana/EN folded), emoji category grouping, swipe gestures (→ bought / ← skip),
  EN/日本語 toggle, purchase-cycle panel.
- **Phase 3 — install**: HTTPS ✅ (`https://spark-d28c.<your-tailnet>.ts.net`).
  Android now has a real installable app (`mobile/`, Capacitor — sideloaded, no
  store); iPhone stays Add-to-Home-Screen. Remaining: wife's iPhone Tailscale
  onboarding, two-phone in-store test.
- **Travel-aware cycles**: ✅ code complete — cycles counted in *days at home*,
  away days read from Google Calendar. Needs the one-time OAuth link below.
- 151/151 tests (`tests/`); live verifications in `test_results/`.

Runs on a home DGX box over a private Tailscale tailnet (bind IP + hostname are
placeholders — swap in your own). Requires Python 3.11+, and a local
OpenAI-compatible LLM endpoint on `:8000` for the enrichment/recipe features
(the list + sync work without it). No cloud, no accounts, no app store.

## Use it

Open **https://spark-d28c.<your-tailnet>.ts.net** from any tailnet device (this is the
URL to Add-to-Home-Screen; plain `http://100.112.171.54:8123` also works in a
browser). Enter your name once.

- **Tap / swipe right** → bought (checked off + counted in purchase history). Undo toast for 8 s.
- **Swipe left** → skip (out of stock; no purchase logged, re-suggested tomorrow).
- **Long-press (hold ~0.6 s)** → item editor: adjust **quantity** and **category**,
  or *remove without buying* / *skip* (neither pollutes the frequency data).
- Works offline in store dead zones — ops queue and flush on reconnect;
  the pill in the header shows synced / syncing / offline.

## Install as an app (Android)

`mobile/` is a Capacitor shell: an installable app with its own icon, off the
store, that opens the very same PWA the DGX serves. The list, the sync, the
offline op queue and the service worker are unchanged — they run at their own
https origin inside the app's WebView, exactly as they do in a browser tab.

That split is deliberate: **changing the UI does not need a new APK.** Edit
`app/index.html`, restart the service, and both phones have it. The APK only
changes when the shell itself does.

The address is **asked for on first launch**, not compiled in — this repo is
public and a tailnet name is not something to publish. Type the same address you
open in the browser; it is remembered per install. If the server can't be
reached the app says so instead of showing a browser error page, and the
hardware Back button from the list returns to that screen, so a wrong address is
always correctable.

The APK is built by GitHub Actions (`.github/workflows/build-apk.yml`) — Google
ships no aarch64 `aapt2`, so an ARM host cannot build it locally. Grab the
`thincart-debug` artifact from the latest run and sideload it:

```bash
gh run download <run-id> --name thincart-debug
adb pair <phone-ip>:<pair-port>        # once, from Developer options → Wireless debugging
adb connect <phone-ip>:<connect-port>  # port rotates; check the phone each time
adb install -r app-debug.apk
```

CI signs with a persistent debug keystore (`DEBUG_KEYSTORE_B64` repo secret) and
fails the build if the signature ever changes — an unexpected key would force a
data-wiping uninstall, taking the saved address and any offline-queued ops with
it.

Shell tests: `cd mobile && npm install && npm test`.

## One-time setup still needed (user actions)

1. ~~Enable Tailscale Serve + HTTPS~~ **done 2026-07-03**:
   `https://spark-d28c.<your-tailnet>.ts.net` → proxy `100.112.171.54:8123`
   (disable with `tailscale serve --https=443 off`).
2. **Wife's iPhone**: install Tailscale from the App Store, sign in (invite her
   or share your account), then open https://spark-d28c.<your-tailnet>.ts.net
   **in Safari** → Share → *Add to Home Screen*. (The Pixel uses the sideloaded
   app instead — see *Install as an app* above.)
3. **Link Google Calendar** (for travel-aware cycles — see below).

## Travel-aware cycles ✈️

A week away is not a week of groceries. Purchase cycles are measured in **days
at home**: days spent out of town are subtracted from every interval, so an
item does not get suggested late just because the household was travelling.
With no away days recorded the arithmetic is identical to before.

**Link the calendar** (read-only, one time, on the DGX):

1. Google Cloud console → new project → enable the **Google Calendar API**
2. *OAuth consent screen* → External → add yourself as a test user
3. *Credentials* → OAuth client ID → **Desktop app**
4. Put the id and secret in `~/.config/thincart/google_oauth.json` (outside this
   repo — it is a bearer credential, and this repo is public):
   ```json
   {"client_id": "….apps.googleusercontent.com", "client_secret": "…",
    "calendar_ids": ["primary"]}
   ```
5. `python3 server/calendar_sync.py --authorize` — open the printed URL in any
   browser; if the browser is on another machine, paste the redirect URL back.

Then `--calendars` lists what the link can read and `--check` prints what reads
as travel without writing anything.

**Detection proposes, you decide.** The calendar has no "travel" field, so
ThinCart flags out-of-office events, all-day events spanning ≥2 days, and
hotel/flight/trip wording; timed events are always days at home. Everything it
finds lands in the ✈️ **Travel** panel for review. A detected day does **not**
affect your cycles until you confirm it — until then it is only shown — and a
day you confirm or reject is never overwritten by a later sync.

Set `THINCART_HOME_PATTERNS` (see `server/deploy/thincart.service`) to the place
words that mean *still home* — without `princeton`, a hotel booked in town
proposed 12 away days that had to be rejected by hand.

## Ops

```bash
systemctl --user status thincart          # service (enable-linger is on)
journalctl --user -u thincart -f          # logs
systemctl --user enable --now thincart-backup.timer   # nightly DB backup 03:30, keep 14
~/Project/_ideas/shopping-list/test_results/           # saved test runs
```

Tests: `python -m pytest tests/` (uses a throwaway DB via `THINCART_DB`).
