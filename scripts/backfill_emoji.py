#!/usr/bin/env python3
"""Give an icon to catalog rows that have none, now rather than overnight.

The work itself lives in `catalog.backfill_emoji()` — the same function the
nightly sweeper calls — so running this proves the production path instead of a
parallel copy of it. Curated map first (free, offline); only what it misses
costs an LLM call, and a row the LLM declines is simply left alone.

Emoji-only writes. Re-running full enrichment would refill category, edibility
and plants from the LLM and quietly overwrite a category set by hand, which is a
worse outcome than a missing icon.

Safe against the live WAL DB while the service is up.

    python3 scripts/backfill_emoji.py            # live DB (server/data/thincart.db)
    THINCART_DB=/path/to.db python3 scripts/backfill_emoji.py
"""

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "server"))

import catalog
from db import connect

ROUNDS = 20  # bounded; ROUNDS x EMOJI_BATCH is far more than any real backlog


async def run() -> int:
    conn = connect()
    lock = asyncio.Lock()
    def count(where: str) -> int:
        return conn.execute(
            f"SELECT COUNT(*) FROM item_catalog WHERE (emoji IS NULL OR emoji = ''){where}"
        ).fetchone()[0]

    print(f"rows without an icon: {count('')}")
    filled = 0
    for _ in range(ROUNDS):
        # Drain the rows nobody has ASKED about yet. Stopping on a zero-fill
        # batch would be wrong: a batch can be 30 legitimate declines while
        # untried rows wait behind them.
        if count(" AND emoji_tried_at IS NULL") == 0:
            break
        filled += await catalog.backfill_emoji(conn, lock)
    left = count("")
    print(f"filled {filled}; {left} still without one")
    if left:
        print("  (a row the LLM declines is gibberish or unpicturable — left as it is,")
        print("   and retried after every other row has had a turn)")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(run()))
