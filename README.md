# PlantCart 🌱

Self-hosted shared shopping list for two phones with real-time sync, plus:
purchase-cycle recommendations ("you're due for milk") and plant-diversity
tracking toward **30 different plants a week**, powered by the local DGX LLM.
See `PLAN.md` for the full architecture (agent-reviewed, approved 2026-07-03).

## Status

- **Phase 0 — real-time sync**: ✅ built, 11/11 contract tests + live two-client
  WS test passing. Service running as systemd user unit on `100.112.171.54:8123`.
- Phase 1 (cycle recommendations), Phase 2 (plants + recipes), Phase 3 (polish): pending.

## Use it

Open `http://100.112.171.54:8123` from any tailnet device. Enter your name once.

- **Tap an item** → bought (checked off + counted in purchase history). Undo toast for 8 s.
- **Long-press** → remove *without* buying (doesn't pollute the frequency data).
- Works offline in store dead zones — ops queue and flush on reconnect;
  the pill in the header shows synced / syncing / offline.

## One-time setup still needed (user actions)

1. **Enable Tailscale Serve** (for HTTPS, required for Add-to-Home-Screen):
   visit https://login.tailscale.com/f/serve?node=nM83T3xSKT11CNTRL and approve,
   also enable **HTTPS Certificates** under DNS in the admin console. Then run:
   `tailscale serve --bg --https=443 http://100.112.171.54:8123`
   → app becomes `https://spark-d28c.<tailnet>.ts.net`
2. **Wife's iPhone**: install Tailscale from the App Store, sign in (invite her
   or share your account), then open the https URL **in Safari** →
   Share → *Add to Home Screen*.

## Ops

```bash
systemctl --user status plantcart          # service (enable-linger is on)
journalctl --user -u plantcart -f          # logs
systemctl --user enable --now plantcart-backup.timer   # nightly DB backup 03:30, keep 14
~/Project/_ideas/shopping-list/test_results/           # saved test runs
```

Tests: `python -m pytest tests/` (uses a throwaway DB via `PLANTCART_DB`).
