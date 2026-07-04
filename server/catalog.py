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

import httpx

import config
import llm

log = logging.getLogger("plantcart.catalog")


async def web_evidence(name: str) -> list[str] | None:
    """Quick SearXNG lookup: top result titles, or None if search is unconfigured
    or down. Used only to judge whether a NEW user-typed item is real vs a typo."""
    if not config.SEARX_URL:
        return None
    try:
        async with httpx.AsyncClient(timeout=6) as client:
            r = await client.get(config.SEARX_URL, params={"q": name, "format": "json"})
            r.raise_for_status()
            return [x.get("title", "") for x in r.json().get("results", [])[:5]]
    except Exception:
        return None

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


def enrich_prompt(display_name: str, existing_names: list[str],
                  evidence: list[str] | None = None) -> str:
    listed = ", ".join(f'"{n}"' for n in existing_names[:200])
    ev = ""
    if evidence is not None:
        ev = ("Web search result titles for this exact text: "
              + json.dumps(evidence, ensure_ascii=False) + "\n")
    return (
        f'Grocery item (Japanese or English): "{display_name}".\n'
        f'Existing catalog items: [{listed}]\n'
        f'{ev}'
        'Reply ONLY JSON: {'
        '"is_real_item": bool — false ONLY if the text looks like a typo or gibberish '
        'rather than a real product (use the web evidence if given; when unsure, true), '
        f'"category": one of {json.dumps(CATEGORIES)}, '
        '"is_edible": bool, '
        '"plants": [the DISTINCT edible plant species a typical serving contains, '
        'as lowercase English tokens, e.g. curry roux -> ["wheat","turmeric","cumin"], '
        'milk -> []], '
        '"english_name": short common English name for this grocery item, '
        '"alias_of": ONLY if this item is the IDENTICAL product as one existing catalog '
        'item written in a different script or spelling (e.g. たまねぎ vs 玉ねぎ), that '
        'exact string. A related/similar/sub-type product is NOT an alias (ミニトマト is '
        'NOT トマト; 冷凍ブロッコリー is NOT ブロッコリー). When unsure: null}'
    )


async def enrich(conn, write_lock, catalog_id: int) -> bool:
    """Enrich one catalog row via the LLM. Returns True if state-visible change."""
    row = conn.execute(
        "SELECT id, canonical_name, display_name, category, aliases_json "
        "FROM item_catalog WHERE id=?",
        (catalog_id,),
    ).fetchone()
    if row is None:
        return False
    # curated rows (seeded: category/aliases pre-set) are authoritative distinct
    # products — never merge them away; only bare user-typed variants may merge
    mergeable = row["category"] is None and row["aliases_json"] == "[]"
    existing = [
        r["canonical_name"]
        for r in conn.execute(
            "SELECT canonical_name FROM item_catalog WHERE id != ? AND llm_enriched_at IS NOT NULL",
            (catalog_id,),
        )
    ]
    # user-typed rows (no curated data) get a quick web search to judge typo-ness;
    # seeded rows are pre-verified — skip the search
    evidence = await web_evidence(row["display_name"]) if mergeable else None
    res = await llm.chat_json(
        enrich_prompt(row["display_name"], existing, evidence), max_tokens=220
    )
    if not isinstance(res, dict):
        return False
    verified = 0 if res.get("is_real_item") is False else 1

    plants = _clean_plants(res.get("plants")) or []
    category = res.get("category") if res.get("category") in CATEGORIES else "other"
    is_edible = 1 if res.get("is_edible") else 0
    alias_of = res.get("alias_of")
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")

    async with write_lock:
        target = None
        if mergeable and alias_of and alias_of != row["canonical_name"]:
            # SAFETY (multi-tenant): a destructive merge repoints items+events for
            # THIS shared catalog row across ALL households. Only allow it when the
            # row is touched by at most one household — i.e. it's a fresh user-typed
            # variant, not a shared corpus row multiple households already use. This
            # stops one tenant's (possibly hallucinated) alias_of from corrupting
            # everyone's history.
            households_touching = {
                r["hh"] for r in conn.execute(
                    "SELECT DISTINCT household_id AS hh FROM purchase_events WHERE catalog_id=? "
                    "UNION SELECT DISTINCT household_id AS hh FROM items WHERE catalog_id=?",
                    (row["id"], row["id"]))
            }
            if len(households_touching) <= 1:
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
            # bank the English name as an alias → EN display + search for JP-typed items
            aliases = json.loads(row["aliases_json"])
            en = res.get("english_name")
            if (isinstance(en, str) and en.strip() and en.isascii()
                    and not any(a.casefold() == en.strip().casefold() for a in aliases)):
                aliases.append(en.strip().lower())
            conn.execute(
                "UPDATE item_catalog SET category=?, is_edible=?, plants_json=?, "
                "aliases_json=?, verified=?, llm_enriched_at=? WHERE id=?",
                (category, is_edible, json.dumps(plants),
                 json.dumps(aliases, ensure_ascii=False), verified, now, row["id"]),
            )
            if not verified:
                log.info("typo-suspect (hidden from candidates): %r", row["display_name"])
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


def weekly_plants(conn, hid: str, now=None, window_days: int = 7) -> list[str]:
    """Distinct plants across a HOUSEHOLD's purchases in the trailing window."""
    now = now or datetime.now(timezone.utc)
    cutoff = (now - timedelta(days=window_days)).isoformat(timespec="seconds")
    plants: set[str] = set()
    for r in conn.execute(
        """SELECT DISTINCT c.plants_json FROM purchase_events e
           JOIN item_catalog c ON c.id = e.catalog_id
           WHERE e.household_id=? AND e.bought_at >= ? AND c.plants_json IS NOT NULL""",
        (hid, cutoff),
    ):
        plants.update(json.loads(r["plants_json"]))
    return sorted(plants)


def recent_purchases(conn, hid: str, days: int = 10) -> list[dict]:
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat(timespec="seconds")
    return [
        dict(r)
        for r in conn.execute(
            """SELECT DISTINCT c.display_name AS name, c.plants_json
               FROM purchase_events e JOIN item_catalog c ON c.id = e.catalog_id
               WHERE e.household_id=? AND e.bought_at >= ? AND c.is_edible = 1""",
            (hid, cutoff),
        )
    ]
