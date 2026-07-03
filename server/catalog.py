"""
catalog.py — LLM enrichment of the item catalog + plant-diversity queries.

Enrichment (async, never blocks an add): category, edibility, the distinct
edible plants an item contributes (curry roux → wheat, turmeric, cumin, …),
and alias detection (たまねぎ ≡ 玉ねぎ → merge onto one catalog row).
Plants are canonical lowercase English tokens — 小麦/Wheat/komugi must not
triple-count (PLAN.md §Intelligence layer 2).
"""
import json
import logging
import re
from datetime import datetime, timedelta, timezone

import llm

log = logging.getLogger("plantcart.catalog")

CATEGORIES = [
    "produce", "dairy", "meat_fish", "pantry", "bakery",
    "frozen", "drinks", "household", "other",
]

_TOKEN = re.compile(r"^[a-z][a-z \-]{0,40}$")


def _clean_plants(raw) -> list[str] | None:
    if not isinstance(raw, list):
        return None
    out = []
    for p in raw:
        if isinstance(p, str):
            tok = p.strip().lower()
            if _TOKEN.match(tok) and tok not in out:
                out.append(tok)
    return out


def enrich_prompt(display_name: str, existing_names: list[str]) -> str:
    listed = ", ".join(f'"{n}"' for n in existing_names[:200])
    return (
        f'Grocery item (Japanese or English): "{display_name}".\n'
        f'Existing catalog items: [{listed}]\n'
        'Reply ONLY JSON: {'
        f'"category": one of {json.dumps(CATEGORIES)}, '
        '"is_edible": bool, '
        '"plants": [the DISTINCT edible plant species a typical serving contains, '
        'as lowercase English tokens, e.g. curry roux -> ["wheat","turmeric","cumin"], '
        'milk -> []], '
        '"alias_of": if this item is THE SAME product as one existing catalog item '
        '(spelling/script variant, e.g. たまねぎ vs 玉ねぎ), that exact string, else null}'
    )


async def enrich(conn, write_lock, catalog_id: int) -> bool:
    """Enrich one catalog row via the LLM. Returns True if state-visible change."""
    row = conn.execute(
        "SELECT id, canonical_name, display_name FROM item_catalog WHERE id=?",
        (catalog_id,),
    ).fetchone()
    if row is None:
        return False
    existing = [
        r["canonical_name"]
        for r in conn.execute(
            "SELECT canonical_name FROM item_catalog WHERE id != ? AND llm_enriched_at IS NOT NULL",
            (catalog_id,),
        )
    ]
    res = await llm.chat_json(enrich_prompt(row["display_name"], existing), max_tokens=200)
    if not isinstance(res, dict):
        return False

    plants = _clean_plants(res.get("plants")) or []
    category = res.get("category") if res.get("category") in CATEGORIES else "other"
    is_edible = 1 if res.get("is_edible") else 0
    alias_of = res.get("alias_of")
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")

    async with write_lock:
        target = None
        if alias_of and alias_of != row["canonical_name"]:
            target = conn.execute(
                "SELECT id, aliases_json FROM item_catalog WHERE canonical_name=?",
                (alias_of,),
            ).fetchone()
        if target:
            # merge: repoint history + live items, record alias, drop this row
            conn.execute("UPDATE items SET catalog_id=? WHERE catalog_id=?",
                         (target["id"], row["id"]))
            conn.execute("UPDATE purchase_events SET catalog_id=? WHERE catalog_id=?",
                         (target["id"], row["id"]))
            aliases = json.loads(target["aliases_json"])
            if row["canonical_name"] not in aliases:
                aliases.append(row["canonical_name"])
            conn.execute("UPDATE item_catalog SET aliases_json=? WHERE id=?",
                         (json.dumps(aliases, ensure_ascii=False), target["id"]))
            conn.execute("DELETE FROM item_catalog WHERE id=?", (row["id"],))
            log.info("alias-merged %r into %r", row["canonical_name"], alias_of)
        else:
            conn.execute(
                "UPDATE item_catalog SET category=?, is_edible=?, plants_json=?, "
                "llm_enriched_at=? WHERE id=?",
                (category, is_edible, json.dumps(plants), now, row["id"]),
            )
        conn.commit()
    return True


async def sweep(conn, write_lock) -> int:
    """Enrich every catalog row the add-time task missed (LLM was down, etc.)."""
    pending = [
        r["id"]
        for r in conn.execute("SELECT id FROM item_catalog WHERE llm_enriched_at IS NULL")
    ]
    done = 0
    for cid in pending:
        if await enrich(conn, write_lock, cid):
            done += 1
    if pending:
        log.info("enrichment sweep: %d/%d", done, len(pending))
    return done


def weekly_plants(conn, now=None, window_days: int = 7) -> list[str]:
    """Distinct plants across purchases in the trailing window (rule-based, no LLM)."""
    now = now or datetime.now(timezone.utc)
    cutoff = (now - timedelta(days=window_days)).isoformat(timespec="seconds")
    plants: set[str] = set()
    for r in conn.execute(
        """SELECT DISTINCT c.plants_json FROM purchase_events e
           JOIN item_catalog c ON c.id = e.catalog_id
           WHERE e.bought_at >= ? AND c.plants_json IS NOT NULL""",
        (cutoff,),
    ):
        plants.update(json.loads(r["plants_json"]))
    return sorted(plants)


def recent_purchases(conn, days: int = 10) -> list[dict]:
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat(
        timespec="seconds"
    )
    return [
        dict(r)
        for r in conn.execute(
            """SELECT DISTINCT c.display_name AS name, c.plants_json
               FROM purchase_events e JOIN item_catalog c ON c.id = e.catalog_id
               WHERE e.bought_at >= ? AND c.is_edible = 1""",
            (cutoff,),
        )
    ]
