"""Curated grocery-item → emoji map for item-specific icons.

The frontend already shows a *category* emoji (produce/dairy/…). This module
gives a per-item icon that looks like the actual thing (avocado→🥑, banana→🍌).
It is the deterministic, offline, zero-latency first pass; the LLM enrichment
(catalog.enrich) fills an emoji for anything not covered here, and the category
emoji remains the final fallback in the UI.

Stdlib only and dependency-free on purpose: db.get_or_create_catalog calls
lookup() at row-creation time, so importing db from here would be circular.
Keys are folded the same way as db.canonical() (NFKC + casefold + whitespace
collapse) so a lookup on a canonical_name matches regardless of width/case.
Only well-supported emoji are used (nothing newer than Unicode 15) so both the
Pixel and the iPhone render them.
"""

import unicodedata

# Raw name → emoji. Keys are folded at import; add both EN and JA where the item
# is commonly typed in either. Exact-match only — compounds and brands
# ("chicken breast", "One Mighty Mill bagel") fall through to the LLM.
_RAW: dict[str, str] = {
    # ── produce: fruit ──
    "avocado": "🥑", "アボカド": "🥑",
    "banana": "🍌", "バナナ": "🍌",
    "apple": "🍎", "red apple": "🍎", "りんご": "🍎", "リンゴ": "🍎",
    "green apple": "🍏",
    "orange": "🍊", "mandarin": "🍊", "みかん": "🍊", "オレンジ": "🍊",
    "lemon": "🍋", "レモン": "🍋",
    "lime": "🍋", "ライム": "🍋",
    "grapes": "🍇", "grape": "🍇", "ぶどう": "🍇", "ブドウ": "🍇",
    "strawberry": "🍓", "strawberries": "🍓", "いちご": "🍓", "イチゴ": "🍓",
    "blueberry": "🫐", "blueberries": "🫐", "ブルーベリー": "🫐",
    "cherry": "🍒", "cherries": "🍒", "さくらんぼ": "🍒",
    "peach": "🍑", "もも": "🍑", "桃": "🍑",
    "pear": "🍐", "なし": "🍐", "梨": "🍐",
    "mango": "🥭", "マンゴー": "🥭",
    "pineapple": "🍍", "パイナップル": "🍍",
    "watermelon": "🍉", "すいか": "🍉", "スイカ": "🍉",
    "melon": "🍈", "メロン": "🍈",
    "kiwi": "🥝", "キウイ": "🥝",
    "coconut": "🥥", "ココナッツ": "🥥",
    # ── produce: veg ──
    "tomato": "🍅", "tomatoes": "🍅", "cherry tomato": "🍅", "トマト": "🍅", "ミニトマト": "🍅",
    "eggplant": "🍆", "aubergine": "🍆", "なす": "🍆", "ナス": "🍆",
    "potato": "🥔", "potatoes": "🥔", "じゃがいも": "🥔", "ジャガイモ": "🥔",
    "sweet potato": "🍠", "さつまいも": "🍠", "サツマイモ": "🍠",
    "carrot": "🥕", "carrots": "🥕", "にんじん": "🥕", "人参": "🥕",
    "corn": "🌽", "とうもろこし": "🌽", "コーン": "🌽",
    "broccoli": "🥦", "ブロッコリー": "🥦",
    "cucumber": "🥒", "きゅうり": "🥒", "キュウリ": "🥒",
    "lettuce": "🥬", "leafy greens": "🥬", "cabbage": "🥬", "spinach": "🥬", "kale": "🥬",
    "レタス": "🥬", "キャベツ": "🥬", "ほうれん草": "🥬", "小松菜": "🥬",
    "onion": "🧅", "onions": "🧅", "たまねぎ": "🧅", "玉ねぎ": "🧅", "玉葱": "🧅",
    "garlic": "🧄", "にんにく": "🧄", "ニンニク": "🧄",
    "ginger": "🫚", "しょうが": "🫚", "生姜": "🫚",
    "bell pepper": "🫑", "green bell pepper": "🫑", "pepper": "🫑", "ピーマン": "🫑", "パプリカ": "🫑",
    "chili": "🌶️", "chili pepper": "🌶️", "hot pepper": "🌶️", "唐辛子": "🌶️",
    "mushroom": "🍄", "mushrooms": "🍄", "きのこ": "🍄", "しいたけ": "🍄",
    "peanut": "🥜", "peanuts": "🥜", "ピーナッツ": "🥜", "落花生": "🥜",
    "edamame": "🫛", "枝豆": "🫛", "えだまめ": "🫛",
    "beans": "🫘", "豆": "🫘",
    # ── dairy & eggs ──
    "milk": "🥛", "牛乳": "🥛", "ミルク": "🥛",
    "egg": "🥚", "eggs": "🥚", "たまご": "🥚", "卵": "🥚",
    "cheese": "🧀", "チーズ": "🧀",
    "butter": "🧈", "バター": "🧈",
    "yogurt": "🥛", "ヨーグルト": "🥛",
    # ── meat & fish ──
    "chicken": "🍗", "poultry": "🍗", "chicken breast": "🍗", "鶏肉": "🍗", "とり肉": "🍗",
    "meat": "🥩", "beef": "🥩", "pork": "🥩", "steak": "🥩", "肉": "🥩", "牛肉": "🥩", "豚肉": "🥩",
    "bacon": "🥓", "ベーコン": "🥓",
    "fish": "🐟", "salmon": "🐟", "魚": "🐟", "さかな": "🐟", "鮭": "🐟",
    "shrimp": "🦐", "prawn": "🦐", "えび": "🦐", "エビ": "🦐",
    "crab": "🦀", "かに": "🦀", "カニ": "🦀",
    "squid": "🦑", "いか": "🦑", "イカ": "🦑",
    "oyster": "🦪", "牡蠣": "🦪", "かき": "🦪",
    "sushi": "🍣", "寿司": "🍣", "すし": "🍣",
    # ── bakery ──
    "bread": "🍞", "パン": "🍞", "食パン": "🍞",
    "baguette": "🥖", "french bread": "🥖", "バゲット": "🥖",
    "bagel": "🥯", "ベーグル": "🥯",
    "croissant": "🥐", "クロワッサン": "🥐",
    "pretzel": "🥨", "プレッツェル": "🥨",
    "pancake": "🥞", "pancakes": "🥞", "パンケーキ": "🥞", "ホットケーキ": "🥞",
    "waffle": "🧇", "ワッフル": "🧇",
    # ── pantry ──
    "rice": "🍚", "white rice": "🍚", "brown rice": "🍚", "米": "🍚", "お米": "🍚", "ごはん": "🍚",
    "pasta": "🍝", "spaghetti": "🍝", "fettuccine": "🍝", "パスタ": "🍝", "スパゲッティ": "🍝",
    "noodles": "🍜", "ramen": "🍜", "udon": "🍜", "soba": "🍜",
    "ラーメン": "🍜", "うどん": "🍜", "そば": "🍜",
    "flour": "🌾", "小麦粉": "🌾",
    "salt": "🧂", "塩": "🧂",
    "olive oil": "🫒", "oil": "🫗", "オリーブオイル": "🫒", "油": "🫗", "サラダ油": "🫗",
    "soy sauce": "🫗", "醤油": "🫗", "しょうゆ": "🫗",
    "honey": "🍯", "はちみつ": "🍯", "蜂蜜": "🍯",
    "jam": "🍓", "ジャム": "🍓",
    "peanut butter": "🥜", "ピーナッツバター": "🥜",
    "chocolate": "🍫", "チョコ": "🍫", "チョコレート": "🍫",
    "cookie": "🍪", "cookies": "🍪", "クッキー": "🍪",
    "candy": "🍬", "あめ": "🍬", "キャンディ": "🍬",
    "popcorn": "🍿", "ポップコーン": "🍿",
    "cereal": "🥣", "シリアル": "🥣",
    "soup": "🍲", "スープ": "🍲",
    "ketchup": "🍅", "ケチャップ": "🍅",
    "canned food": "🥫", "缶詰": "🥫",
    # ── drinks ──
    "water": "💧", "水": "💧",
    "juice": "🧃", "apple juice": "🧃", "orange juice": "🧃", "ジュース": "🧃",
    "coffee": "☕", "コーヒー": "☕",
    "tea": "🍵", "green tea": "🍵", "お茶": "🍵", "緑茶": "🍵", "紅茶": "🍵",
    "soda": "🥤", "cola": "🥤", "コーラ": "🥤", "炭酸": "🥤",
    "beer": "🍺", "ビール": "🍺",
    "wine": "🍷", "ワイン": "🍷",
    "sake": "🍶", "日本酒": "🍶", "酒": "🍶",
    # ── household ──
    "toilet paper": "🧻", "tissue": "🧻", "tissues": "🧻", "paper towel": "🧻",
    "トイレットペーパー": "🧻", "ティッシュ": "🧻", "キッチンペーパー": "🧻",
    "soap": "🧼", "石鹸": "🧼", "せっけん": "🧼",
    "detergent": "🧴", "shampoo": "🧴", "洗剤": "🧴", "シャンプー": "🧴",
    "sponge": "🧽", "スポンジ": "🧽",
    "battery": "🔋", "batteries": "🔋", "電池": "🔋",
}


def _fold(name: str) -> str:
    """Match db.canonical(): NFKC fold, casefold, collapse whitespace."""
    return " ".join(unicodedata.normalize("NFKC", name).casefold().split())


EMOJI: dict[str, str] = {_fold(k): v for k, v in _RAW.items()}


def lookup(canonical_name: str) -> str | None:
    """Return the curated emoji for a (already- or not-yet-canonical) name, or None."""
    return EMOJI.get(_fold(canonical_name))


def is_emoji(s) -> bool:
    """Cheap validation for an LLM-supplied emoji: short, non-ASCII, no letters/digits.

    Emoji can be multi-codepoint (ZWJ sequences, skin-tone, VS-16), so allow a few
    code points but reject anything that is plainly text.
    """
    if not isinstance(s, str):
        return False
    s = s.strip()
    if not s or s.isascii() or len(s) > 8:
        return False
    return not any(c.isalnum() for c in s)
