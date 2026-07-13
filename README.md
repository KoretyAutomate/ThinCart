# PlantCart 🌱

Self-hosted shared shopping list for two phones with real-time sync, plus:
purchase-cycle recommendations ("you're due for milk") and plant-diversity
tracking toward **30 different plants a week**, powered by the local DGX LLM.
See `PLAN.md` for the full architecture (agent-reviewed, approved 2026-07-03).

## Status

- **Phase 0 — real-time sync**: ✅ live (idempotent ops, WS broadcast, offline queue).
- **Phase 1 — cycle recommendations**: ✅ live (suggested tray wakes after ~3 buys/item).
- **Phase 2 — plants + recipes**: ✅ live (LLM enrichment, 🌱 counter, ideas panel).
- **Candidates + categories**: ✅ live — 173-item seeded catalog, typing dropdown
  (kana/EN folded), emoji category grouping, swipe gestures (→ bought / ← skip),
  EN/日本語 toggle, purchase-cycle panel.
- **Phase 3 — install**: HTTPS ✅ (`https://spark-d28c.<your-tailnet>.ts.net`); remaining:
  A2HS both phones, wife's iPhone Tailscale onboarding, two-phone in-store test.
- 35/35 tests (`tests/`); live verifications in `test_results/`.

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

## One-time setup still needed (user actions)

1. ~~Enable Tailscale Serve + HTTPS~~ **done 2026-07-03**:
   `https://spark-d28c.<your-tailnet>.ts.net` → proxy `100.112.171.54:8123`
   (disable with `tailscale serve --https=443 off`).
2. **Wife's iPhone**: install Tailscale from the App Store, sign in (invite her
   or share your account), then open https://spark-d28c.<your-tailnet>.ts.net
   **in Safari** → Share → *Add to Home Screen*. Do the same on the Pixel
   (Chrome → Install app).

## Ops

```bash
systemctl --user status plantcart          # service (enable-linger is on)
journalctl --user -u plantcart -f          # logs
systemctl --user enable --now plantcart-backup.timer   # nightly DB backup 03:30, keep 14
~/Project/_ideas/shopping-list/test_results/           # saved test runs
```

Tests: `python -m pytest tests/` (uses a throwaway DB via `PLANTCART_DB`).
