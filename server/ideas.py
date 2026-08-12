"""
ideas.py — recipes + plant-diversity suggestions (PLAN.md §Intelligence layer 2).

Split out of app.py: the LLM feature is self-contained (two prompts, one cached
endpoint) and app.py had grown past the size the repo's quality ceiling allows.
A pure move — the prompts, the cache TTL and the post-filter are unchanged.

Everything here is best-effort. The list and its sync never depend on the DGX
LLM being up; when it is not, this endpoint 503s and nothing else notices.

The SQLite connection is handed over by `bind()` at startup rather than
imported from app.py, which would be circular.
"""

import asyncio
import json
import logging
import sqlite3
from datetime import datetime, UTC

from fastapi import APIRouter, HTTPException

import catalog
import db
import llm
import plants

log = logging.getLogger("thincart.ideas")
router = APIRouter()

TTL_H = 6
cache: dict = {"data": None, "at": None}

_conn: sqlite3.Connection | None = None


def bind(conn: sqlite3.Connection) -> None:
    global _conn
    _conn = conn


def _need() -> sqlite3.Connection:
    if _conn is None:  # pragma: no cover — a wiring error, not a runtime path
        raise RuntimeError("ideas.bind() was never called")
    return _conn


def _recipes_prompt(available, on_list, week_plants) -> str:
    return (
        "You help a Japanese/English bilingual household diversify toward 30 different "
        f"edible plants per week. Plants eaten this week: {json.dumps(week_plants)}.\n"
        f"Ingredients they have (bought recently): {json.dumps(available, ensure_ascii=False)}.\n"
        f"Already on their shopping list: {json.dumps(on_list, ensure_ascii=False)}.\n"
        'Suggest 3 easy dinner recipes. Reply ONLY JSON: {"recipes": [{'
        '"title": str (English, may add Japanese in parens), '
        '"uses": [ingredients they already have], '
        '"missing": [1-3 grocery items to buy, in the language the ingredient is usually '
        "listed on their list], "
        '"new_plants": [lowercase English plant tokens this adds beyond their week]}]}'
    )


def _diversity_prompt(recent_plants) -> str:
    return (
        f"A household ate these plants recently: {json.dumps(recent_plants)}.\n"
        "Suggest 8 DIFFERENT edible plants, common in Japanese supermarkets, to broaden "
        'their variety toward 30 plants/week. Reply ONLY JSON: {"suggestions": [{'
        '"plant": lowercase English plant token, '
        '"buy": the concrete grocery item to put on the list, in Japanese}]}'
    )


@router.get("/api/ideas")
async def get_ideas(refresh: int = 0):
    """Recipes + diversity suggestions (LLM). Cached; failure never blocks the list."""
    if not refresh and cache["data"] and (datetime.now(UTC) - cache["at"]).total_seconds() < TTL_H * 3600:
        return cache["data"]

    conn = _need()
    available = [p["name"] for p in catalog.recent_purchases(conn)]
    on_list = [i["name"] for i in db.state(conn)["items"]]
    week = catalog.weekly_plants(conn)
    month = catalog.weekly_plants(conn, window_days=30)

    recipes, diversity = await asyncio.gather(
        llm.chat_json(_recipes_prompt(available, on_list, week), max_tokens=700, timeout=90),
        llm.chat_json(_diversity_prompt(month), max_tokens=400, timeout=90),
    )
    recipes = recipes.get("recipes") if isinstance(recipes, dict) else None
    diversity = diversity.get("suggestions") if isinstance(diversity, dict) else None
    if diversity is not None:
        # the LLM sometimes suggests plants just eaten despite the prompt —
        # enforce "different" deterministically against the 30-day set. Canonicalize
        # the suggestion first, or a synonym ("capsicum" for an eaten "bell pepper")
        # walks straight through the filter.
        eaten = set(month)
        diversity = [
            s
            for s in diversity
            if isinstance(s, dict) and s.get("buy") and not set(plants.normalize([str(s.get("plant", ""))])) & eaten
        ]
    if recipes is None and diversity is None:
        raise HTTPException(503, "LLM unavailable — list and sync are unaffected")

    data = {
        "recipes": recipes or [],
        "diversity": diversity or [],
        "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
    }
    cache["data"], cache["at"] = data, datetime.now(UTC)
    return data
