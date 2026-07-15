#!/usr/bin/env python3
"""Backfill item_catalog.emoji from the curated map for rows that have none.

Safe to run against the live WAL DB while the service is up (one quick write txn).
The LLM enrichment fills icons for anything the curated map misses on its own; this
just gives the common items their icon immediately instead of waiting for a re-enrich.

    python3 scripts/backfill_emoji.py            # live DB (server/data/thincart.db)
    THINCART_DB=/path/to.db python3 scripts/backfill_emoji.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "server"))

import emoji  # noqa: E402
from db import connect  # noqa: E402


def main() -> int:
    conn = connect()
    rows = conn.execute(
        "SELECT id, canonical_name, display_name FROM item_catalog "
        "WHERE emoji IS NULL OR emoji = ''"
    ).fetchall()
    filled = 0
    for r in rows:
        e = emoji.lookup(r["canonical_name"]) or emoji.lookup(r["display_name"])
        if e:
            conn.execute("UPDATE item_catalog SET emoji=? WHERE id=?", (e, r["id"]))
            filled += 1
            print(f"  {r['display_name']!r} -> {e}")
    conn.commit()
    print(f"backfilled {filled}/{len(rows)} unset rows")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
