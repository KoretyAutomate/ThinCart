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

import emoji
import llm
import plants
from db import canonical

log = logging.getLogger("thincart.catalog")

SEARX_URL = "http://127.0.0.1:8080/search"


async def web_evidence(name: str) -> list[str] | None:
    """Quick local-SearXNG lookup: top result titles, or None if search is down.
    Used only to judge whether a NEW user-typed item is real vs a typo."""
    try:
        async with httpx.AsyncClient(timeout=6) as client:
            r = await client.get(SEARX_URL, params={"q": name, "format": "json"})
            r.raise_for_status()
            return [x.get("title", "") for x in r.json().get("results", [])[:5]]
    except Exception:
        return None

CATEGORIES = [
    "produce", "dairy", "meat_fish", "pantry", "bakery",
    "frozen", "drinks", "household", "other",
]


def _is_variety(source_canon: str, target_names: list[str]) -> bool:
    """Deterministic backstop for the alias merge: True if the new item is a
    more-specific variety/brand of the merge target — its word set strictly
    CONTAINS a target name's word set ('white rice' ⊃ 'rice', 'fettuccine pasta'
    ⊃ 'pasta', 'green bell pepper' ⊃ 'bell pepper'). Such items must stay their
    own catalog row no matter what the LLM says."""
    src = set(source_canon.split())
    for name in target_names:
        tw = set(canonical(name).split())
        if tw and tw < src:            # all target words present + extra qualifier
            return True
    return False


def item_context(row) -> str:
    """The item's own text — what disambiguates a bare LLM token ("pepper" on
    `bell pepper bag` is a capsicum; on `Amys frozen pizza` it is the spice)."""
    parts = [row["canonical_name"], row["display_name"]]
    try:
        parts += json.loads(row["aliases_json"] or "[]")
    except (KeyError, IndexError, TypeError, ValueError):
        pass
    return " ".join(str(p) for p in parts if p)


def _clean_plants(raw, context: str = "") -> list[str] | None:
    """Raw LLM plant list → canonical vocabulary. The prompt asks for canonical
    tokens; this is the safety net that makes sure it got them (the local LLM is
    flaky, and one drifted token silently double-counts the week)."""
    if not isinstance(raw, list):
        return None
    return plants.normalize(raw, context)


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
        f'{plants.VOCAB_RULES}\n'
        'Reply ONLY JSON: {'
        '"is_real_item": bool — false ONLY if the text looks like a typo or gibberish '
        'rather than a real product (use the web evidence if given; when unsure, true), '
        f'"category": one of {json.dumps(CATEGORIES)}, '
        '"emoji": a single emoji that best pictures THIS specific item '
        '(🥑 avocado, 🍌 banana, 🥯 bagel, 🍝 pasta, 🐟 salmon); null if none fits, '
        '"is_edible": bool, '
        '"plants": [the DISTINCT edible plant species a typical serving contains, '
        'as canonical tokens per the PLANT TOKEN RULES above. Examples: '
        'curry roux -> ["wheat","turmeric","cumin","coriander"]; '
        'green bell pepper -> ["bell pepper"]; frozen pizza -> '
        '["wheat","tomato","onion","garlic","basil","oregano","black pepper"]; '
        'lemon -> ["lemon"]; yellow squash -> ["summer squash"]; milk -> []], '
        '"english_name": the common English name for THIS specific item — PRESERVE '
        'brand names and the specific type/variety, never generalize. '
        '"One Mighty Mill bagel" -> "one mighty mill bagel" (NOT "bagel"); '
        '"white rice" -> "white rice" (NOT "rice"); "fettuccine" -> "fettuccine" '
        '(NOT "pasta"); "yellow squash" -> "yellow squash" (NOT "zucchini"), '
        '"alias_of": ONLY if this item is the IDENTICAL product as one existing catalog '
        'item written in a different script/spelling or an exact translation '
        '(たまねぎ vs 玉ねぎ; ﾐﾙｸ vs ミルク; coriander vs cilantro; aubergine vs eggplant). '
        'A different TYPE, VARIETY, BRAND, or SUB-TYPE is NOT an alias — keep it separate: '
        'white rice is NOT rice/米; brown rice is NOT rice; spaghetti and fettuccine are '
        'NOT pasta/パスタ (they are types of pasta); yellow squash is NOT zucchini/ズッキーニ '
        '(different vegetable); "One Mighty Mill bagel" is NOT bagel/ベーグル (it is a brand); '
        'ミニトマト is NOT トマト; 冷凍ブロッコリー is NOT ブロッコリー. When unsure: null}'
    )


async def enrich(conn, write_lock, catalog_id: int) -> bool:
    """Enrich one catalog row via the LLM. Returns True if state-visible change."""
    row = conn.execute(
        "SELECT id, canonical_name, display_name, category, aliases_json, emoji, "
        "note, budget, preferred_store_id FROM item_catalog WHERE id=?",
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

    item_plants = _clean_plants(res.get("plants"), item_context(row)) or []
    category = res.get("category") if res.get("category") in CATEGORIES else "other"
    is_edible = 1 if res.get("is_edible") else 0
    alias_of = res.get("alias_of")
    # curated map wins; only ask the LLM to fill an icon we don't already have
    emoji_val = row["emoji"] or emoji.lookup(row["canonical_name"])
    if not emoji_val and emoji.is_emoji(res.get("emoji")):
        emoji_val = res["emoji"].strip()
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")

    async with write_lock:
        target = None
        if mergeable and alias_of and alias_of != row["canonical_name"]:
            target = conn.execute(
                "SELECT id, aliases_json FROM item_catalog WHERE canonical_name=?",
                (alias_of,),
            ).fetchone()
        if target and _is_variety(
            row["canonical_name"], [alias_of] + json.loads(target["aliases_json"])
        ):
            log.info("merge blocked (variety/brand): %r kept distinct from %r",
                     row["canonical_name"], alias_of)
            target = None
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
            # criteria the user set on the doomed row before this async merge ran
            # must survive it — carry note/budget/preferred store (target wins)
            conn.execute(
                "UPDATE item_catalog SET "
                "note = CASE WHEN note='' THEN ? ELSE note END, "
                "budget = COALESCE(budget, ?), "
                "preferred_store_id = COALESCE(preferred_store_id, ?) WHERE id=?",
                (row["note"], row["budget"], row["preferred_store_id"], target["id"]),
            )
            conn.execute("DELETE FROM item_catalog WHERE id=?", (row["id"],))
            log.info("alias-merged %r into %r", row["canonical_name"], alias_of)
        else:
            # bank the English name as an alias → EN display + search for JP-typed
            # items ONLY. For an English-typed item the display already IS English
            # and name_en() shows it verbatim; banking a generic english_name here
            # would both shadow the user's specific name and mis-route future adds.
            aliases = json.loads(row["aliases_json"])
            en = res.get("english_name")
            if (not row["display_name"].isascii()
                    and isinstance(en, str) and en.strip() and en.isascii()
                    and not any(a.casefold() == en.strip().casefold() for a in aliases)):
                aliases.append(en.strip().lower())
            conn.execute(
                "UPDATE item_catalog SET category=?, is_edible=?, plants_json=?, "
                "aliases_json=?, verified=?, emoji=?, llm_enriched_at=? WHERE id=?",
                (category, is_edible, json.dumps(item_plants),
                 json.dumps(aliases, ensure_ascii=False), verified, emoji_val, now,
                 row["id"]),
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


def weekly_plants(conn, now=None, window_days: int = 7) -> list[str]:
    """Distinct canonical plants across purchases in the trailing window.

    Rule-based, no LLM. Tokens are re-canonicalized on read: rows enriched before
    the vocabulary existed (or by an LLM that ignored it) must not double-count,
    and the count has to be right even when the DGX is down and no re-enrichment
    is possible. Zero-weight tokens (sugarcane) are not plants you ate.
    """
    now = now or datetime.now(timezone.utc)
    cutoff = (now - timedelta(days=window_days)).isoformat(timespec="seconds")
    found: set[str] = set()
    for r in conn.execute(
        """SELECT DISTINCT c.plants_json, c.canonical_name, c.display_name, c.aliases_json
           FROM purchase_events e JOIN item_catalog c ON c.id = e.catalog_id
           WHERE e.bought_at >= ? AND c.plants_json IS NOT NULL""",
        (cutoff,),
    ):
        found.update(
            plants.normalize(json.loads(r["plants_json"]), item_context(r))
        )
    return sorted(plants.countable(found))


def weekly_score(conn, now=None, window_days: int = 7) -> float:
    """Weighted plant points for the trailing window (herbs/spices score ¼)."""
    return plants.score(weekly_plants(conn, now, window_days))


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
