"""
ops.py — the wire format of a mutation.

Every change to the list arrives as one Op: the phones queue them offline and
POST them when there is signal, so this model IS the sync contract. It lives
apart from app.py because it is pure declaration, and because app.py crossed the
repo's 600-line ceiling when Phase 6 added the store-identity and product-pick
fields (same reason ideas.py was split out).

Fields are deliberately permissive and almost all optional: an op written by an
older build of the app must still apply, and a phone that has been in a dead
zone for a day is exactly the case this has to survive.
"""

from typing import Literal

from pydantic import BaseModel, Field


class Op(BaseModel):
    op_id: str = Field(..., min_length=8, max_length=64)
    type: Literal[
        "add",
        "checkoff",
        "remove",
        "skip",
        "undo_checkoff",
        "undo_purchase",
        "snooze",
        "edit",
        "store_upsert",
        "store_delete",
        "product_pick",
    ]
    actor: str = Field("", max_length=40)
    # add / edit
    name: str | None = Field(None, max_length=120)
    qty_note: str | None = Field(None, max_length=120)
    # edit (category adjustment — one of catalog.CATEGORIES)
    category: str | None = Field(None, max_length=20)
    # edit: persistent purchase criteria (catalog-level — survive checkoff→re-add)
    note: str | None = Field(None, max_length=200)
    # edit: typical price. STRING on the wire: "" clears; "３００円"/"¥1,200" parse
    budget: str | None = Field(None, max_length=20)
    # edit / checkoff: store display name ("" clears the preference on edit)
    store: str | None = Field(None, max_length=60)
    # store_upsert
    store_name: str | None = Field(None, max_length=60)
    store_notes: str | None = Field(None, max_length=300)
    # store_upsert (Phase 6): the real-world place this store IS, from the
    # OpenStreetMap picker. All optional — a store added as free text has none of
    # them and works exactly as it did before.
    store_osm_id: str | None = Field(None, max_length=40)
    store_address: str | None = Field(None, max_length=300)
    store_lat: float | None = None
    store_lon: float | None = None
    store_brand: str | None = Field(None, max_length=60)
    # store_upsert (Phase 6): the CHAIN's own id for this branch, which is what
    # its product data is keyed by. Distinct from osm_id, and usually absent.
    store_chain: str | None = Field(None, max_length=30)
    store_chain_id: str | None = Field(None, max_length=20)
    # product_pick: which actual product the household buys for a catalog item.
    # "milk" cannot be priced; "Wegmans Organic Creamy Sunflower Butter 16oz" can.
    pick_chain: str | None = Field(None, max_length=30)
    pick_sku: str | None = Field(None, max_length=40)
    pick_name: str | None = Field(None, max_length=200)
    pick_brand: str | None = Field(None, max_length=80)
    pick_size: str | None = Field(None, max_length=40)
    # store_delete
    store_id: int | None = None
    # add (client-generated item uuid) / checkoff / remove
    item_id: str | None = Field(None, min_length=8, max_length=64)
    # undo_checkoff: the op_id of the checkoff being undone
    target_op_id: str | None = Field(None, max_length=64)
    # undo_purchase: the purchase_events.id being corrected from the History panel
    event_id: int | None = None
    # snooze (suggestion dismissal — server-side so it silences BOTH phones)
    # edit fallback: lets criteria apply when the item row vanished mid-edit
    catalog_id: int | None = None
