# PlantCart — native mobile wrapper (Capacitor)

Scaffolding to ship the PlantCart PWA as native **Android** and **iOS** apps.
The web assets in `../app/` are **bundled** into the app; the app then talks to
the **hosted** PlantCart API over the network (there is no server inside the
app). This directory contains only scaffolding + docs — no `npm install` or
Capacitor build has been run here.

## Layout

| File | Purpose |
|---|---|
| `capacitor.config.json` | Capacitor config. `appId=com.plantcart.app`, `appName=PlantCart`, `webDir=www`, bundled assets (no remote-URL wrapper), `androidScheme=https`, `cleartext=false`. |
| `package.json` | Capacitor deps (core, cli, android, ios, local-notifications) + scripts. |
| `api-base.js` | Build-time shim that publishes `window.PLANTCART_API_BASE`. Copied into `www/` by `copy-web`. |
| `local-notifications.md` | Design + JS sketch for on-device due-cycle reminders (the native capability for Apple 4.2). |
| `SUBMISSION.md` | Full Google Play + Apple App Store runbook. |

The Android CI build lives at `../.github/workflows/build-android.yml` (x86-64
GitHub Actions, since the aarch64 DGX can't run Android's x86-64 tooling).

## npm scripts

- `npm run copy-web` — wipe/recreate `www/`, copy `../app/.` into it, then drop
  `api-base.js` in as `www/api-base.js`.
- `npm run sync` — `copy-web` then `cap sync` (copies web + plugins into native
  projects).
- `npm run build:android` — `sync` then `gradlew assembleDebug`.
- `npm run open:ios` — `sync` then `cap open ios` (macOS + Xcode only).

## First-time setup (not run here)

```bash
cd mobile
npm install
npx cap add android        # generates android/  (also done lazily in CI)
npx cap add ios            # macOS only, generates ios/
npm run sync
```

## How the native build points at the remote API

The PWA makes **same-origin** calls today (`fetch('/api/...')`,
`new WebSocket(...location.host...)`). That only works on the web because the
FastAPI server serves `index.html` itself. In a bundled app the "origin" is
`capacitor://localhost` / `https://localhost` — there's nothing there — so the
app must target the **remote** PlantCart server.

Mechanism:

1. **`api-base.js` sets a global** before the app loads:
   ```js
   window.PLANTCART_API_BASE = 'https://api.plantcart.example.com';
   ```
   Edit this to your production API origin (no trailing slash) before
   `npm run sync`. `copy-web` copies it into `www/`.

2. **Load it before the app code.** The bundled `www/index.html` must include
   the shim ahead of its inline app script:
   ```html
   <script src="api-base.js"></script>   <!-- native build only -->
   ```
   On the **web** build the file is absent / the global is unset, so the app
   falls back to same-origin.

3. **The app reads the global** via a small `apiUrl()` / `wsUrl()` helper — see
   the contract in `api-base.js`. **This helper is NOT wired into `index.html`
   yet**: honoring `window.PLANTCART_API_BASE` (falling back to same-origin) is
   the one required web-layer code change before a store build, tracked as a
   follow-up in `SUBMISSION.md` §0.

Because native requests are cross-origin to the API, the backend must send CORS
for the app origin and the JWT must travel as `Authorization: Bearer <jwt>`
(the app's existing scheme), not a cookie.

## Status

Scaffolding + docs only. Follow-ups before a real store build:
- Wire `index.html` to honor `window.PLANTCART_API_BASE` (§0 of `SUBMISSION.md`).
- Implement the local due-cycle notifications (`local-notifications.md`).
- Fill in and host `../PRIVACY.md`.
