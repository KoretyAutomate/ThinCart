/*
 * ThinCart — API base shim for the native (Capacitor) build.
 * ============================================================
 *
 * WHY THIS EXISTS
 * ---------------
 * The web PWA (app/index.html) talks to the backend with SAME-ORIGIN calls:
 *
 *     fetch('/api/catalog')                              // relative -> current origin
 *     new WebSocket(`${proto}://${location.host}/ws`)    // current host
 *
 * That works on the web because the FastAPI server serves index.html itself,
 * so "same origin" == "the ThinCart server". Inside a store-shipped native
 * app there is NO local server: the web assets are BUNDLED and served from
 * capacitor://localhost (iOS) or https://localhost (Android). Same-origin
 * calls would resolve to that local scheme and there is nothing there to
 * answer them. The native build must therefore point every /api/* and /ws
 * call at a configured REMOTE backend (the hosted ThinCart server).
 *
 * This shim publishes a single global, `window.THINCART_API_BASE`, that the
 * web layer reads to decide where to send requests. It is loaded BEFORE the
 * app code (see README for the <script> ordering) and copied into www/ by the
 * `copy-web` npm script.
 *
 * SET THE VALUE AT BUILD TIME
 * ---------------------------
 * Replace the placeholder below with the production API origin before running
 * `npm run sync` / `build:android` / `open:ios`. No trailing slash. Example:
 *
 *     window.THINCART_API_BASE = 'https://api.thincart.example.com';
 *
 * On the WEB build this file is absent (or THINCART_API_BASE is left unset),
 * so the app falls back to same-origin — see the helper contract below.
 *
 * WHAT THE WEB LAYER MUST DO (documented follow-up — NOT YET WIRED)
 * ----------------------------------------------------------------
 * index.html currently hardcodes relative URLs. To honor this shim, the web
 * layer needs a tiny helper that both the web and native builds share. This
 * is the ONE required code change to app/index.html before a store build,
 * and it is intentionally left as a follow-up (see SUBMISSION.md):
 *
 *     const API_BASE = (window.THINCART_API_BASE || '').replace(/\/$/, '');
 *     const apiUrl = p => API_BASE + p;                 // apiUrl('/api/cycles')
 *     const wsUrl  = () => {
 *       if (API_BASE) return API_BASE.replace(/^http/, 'ws') + '/ws';
 *       const proto = location.protocol === 'https:' ? 'wss' : 'ws';
 *       return `${proto}://${location.host}/ws`;
 *     };
 *
 * Then every `fetch('/api/...')` becomes `fetch(apiUrl('/api/...'), ...)` and
 * the WebSocket constructor uses `wsUrl()`. With THINCART_API_BASE unset the
 * expressions collapse back to the exact same-origin behavior the web build
 * has today, so a single index.html serves BOTH targets.
 *
 * Note: because native calls are cross-origin to the remote API, the backend
 * must send permissive CORS for the app origin (capacitor://localhost /
 * https://localhost) and the auth token must travel as an `Authorization:
 * Bearer <jwt>` header (already the app's scheme) rather than a cookie.
 */
window.THINCART_API_BASE = 'https://api.thincart.example.com';
