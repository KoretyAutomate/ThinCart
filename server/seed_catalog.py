"""
seed_catalog.py — one-shot starter catalog so typing candidates work from day one.

~150 common Japanese grocery items with curated categories + search aliases
(kana variants + English). plants_json is left NULL so the normal LLM
enrichment (add-time + nightly sweep) fills it. Idempotent: existing
canonical names are skipped, so user-created rows are never touched.

Run:  python3 seed_catalog.py
"""
import json

import db

# (display_name, category, is_edible, aliases)
SEED = [
    # ── produce ──
    ("玉ねぎ", "produce", 1, ["たまねぎ", "タマネギ", "onion"]),
    ("にんじん", "produce", 1, ["人参", "ニンジン", "carrot"]),
    ("じゃがいも", "produce", 1, ["ジャガイモ", "馬鈴薯", "potato"]),
    ("キャベツ", "produce", 1, ["cabbage"]),
    ("レタス", "produce", 1, ["lettuce"]),
    ("トマト", "produce", 1, ["tomato"]),
    ("ミニトマト", "produce", 1, ["プチトマト", "cherry tomato"]),
    ("きゅうり", "produce", 1, ["キュウリ", "胡瓜", "cucumber"]),
    ("なす", "produce", 1, ["ナス", "茄子", "eggplant"]),
    ("ピーマン", "produce", 1, ["bell pepper", "green pepper"]),
    ("パプリカ", "produce", 1, ["paprika"]),
    ("ほうれん草", "produce", 1, ["ホウレンソウ", "ほうれんそう", "spinach"]),
    ("小松菜", "produce", 1, ["こまつな", "コマツナ", "komatsuna"]),
    ("白菜", "produce", 1, ["はくさい", "ハクサイ", "napa cabbage"]),
    ("大根", "produce", 1, ["だいこん", "ダイコン", "daikon", "radish"]),
    ("かぶ", "produce", 1, ["カブ", "蕪", "turnip"]),
    ("ごぼう", "produce", 1, ["ゴボウ", "牛蒡", "burdock"]),
    ("れんこん", "produce", 1, ["レンコン", "蓮根", "lotus root"]),
    ("ブロッコリー", "produce", 1, ["broccoli"]),
    ("カリフラワー", "produce", 1, ["cauliflower"]),
    ("もやし", "produce", 1, ["モヤシ", "bean sprouts"]),
    ("長ねぎ", "produce", 1, ["ねぎ", "ネギ", "長ネギ", "green onion", "leek"]),
    ("小ねぎ", "produce", 1, ["万能ねぎ", "細ねぎ", "scallion"]),
    ("にんにく", "produce", 1, ["ニンニク", "garlic"]),
    ("生姜", "produce", 1, ["しょうが", "ショウガ", "ginger"]),
    ("かぼちゃ", "produce", 1, ["カボチャ", "南瓜", "pumpkin", "kabocha"]),
    ("さつまいも", "produce", 1, ["サツマイモ", "薩摩芋", "sweet potato"]),
    ("里芋", "produce", 1, ["さといも", "サトイモ", "taro"]),
    ("山芋", "produce", 1, ["長芋", "ながいも", "yam"]),
    ("アボカド", "produce", 1, ["avocado"]),
    ("オクラ", "produce", 1, ["okra"]),
    ("アスパラガス", "produce", 1, ["アスパラ", "asparagus"]),
    ("ズッキーニ", "produce", 1, ["zucchini"]),
    ("セロリ", "produce", 1, ["celery"]),
    ("ニラ", "produce", 1, ["にら", "韮", "chives", "nira"]),
    ("水菜", "produce", 1, ["みずな", "ミズナ", "mizuna"]),
    ("春菊", "produce", 1, ["しゅんぎく", "シュンギク", "shungiku"]),
    ("チンゲン菜", "produce", 1, ["ちんげんさい", "青梗菜", "bok choy"]),
    ("とうもろこし", "produce", 1, ["トウモロコシ", "コーン", "corn"]),
    ("枝豆", "produce", 1, ["えだまめ", "エダマメ", "edamame"]),
    ("しめじ", "produce", 1, ["シメジ", "shimeji"]),
    ("えのき", "produce", 1, ["エノキ", "enoki"]),
    ("しいたけ", "produce", 1, ["シイタケ", "椎茸", "shiitake"]),
    ("まいたけ", "produce", 1, ["マイタケ", "舞茸", "maitake"]),
    ("エリンギ", "produce", 1, ["eringi", "king oyster mushroom"]),
    ("マッシュルーム", "produce", 1, ["mushroom"]),
    ("バナナ", "produce", 1, ["banana"]),
    ("りんご", "produce", 1, ["リンゴ", "林檎", "apple"]),
    ("みかん", "produce", 1, ["ミカン", "蜜柑", "mandarin", "orange"]),
    ("いちご", "produce", 1, ["イチゴ", "苺", "strawberry"]),
    ("ぶどう", "produce", 1, ["ブドウ", "葡萄", "grape"]),
    ("レモン", "produce", 1, ["lemon"]),
    ("キウイ", "produce", 1, ["kiwi"]),
    ("梨", "produce", 1, ["なし", "ナシ", "pear"]),
    ("桃", "produce", 1, ["もも", "モモ", "peach"]),
    ("ブルーベリー", "produce", 1, ["blueberry"]),
    # ── meat & fish ──
    ("鶏むね肉", "meat_fish", 1, ["鶏胸肉", "むね肉", "chicken breast"]),
    ("鶏もも肉", "meat_fish", 1, ["もも肉", "chicken thigh"]),
    ("ささみ", "meat_fish", 1, ["ササミ", "chicken tender"]),
    ("手羽先", "meat_fish", 1, ["てばさき", "chicken wings"]),
    ("豚こま切れ", "meat_fish", 1, ["豚こま", "pork offcuts"]),
    ("豚バラ肉", "meat_fish", 1, ["豚ばら", "pork belly"]),
    ("豚ロース", "meat_fish", 1, ["pork loin"]),
    ("牛こま切れ", "meat_fish", 1, ["牛こま", "beef offcuts"]),
    ("牛ステーキ肉", "meat_fish", 1, ["ステーキ", "steak"]),
    ("豚ひき肉", "meat_fish", 1, ["豚挽き肉", "ground pork"]),
    ("鶏ひき肉", "meat_fish", 1, ["鶏挽き肉", "ground chicken"]),
    ("合いびき肉", "meat_fish", 1, ["合挽き肉", "ground meat mix"]),
    ("ベーコン", "meat_fish", 1, ["bacon"]),
    ("ハム", "meat_fish", 1, ["ham"]),
    ("ソーセージ", "meat_fish", 1, ["ウインナー", "sausage"]),
    ("鮭", "meat_fish", 1, ["さけ", "サーモン", "salmon"]),
    ("さば", "meat_fish", 1, ["サバ", "鯖", "mackerel"]),
    ("さんま", "meat_fish", 1, ["サンマ", "秋刀魚", "saury"]),
    ("ぶり", "meat_fish", 1, ["ブリ", "鰤", "yellowtail"]),
    ("まぐろ", "meat_fish", 1, ["マグロ", "鮪", "tuna"]),
    ("たら", "meat_fish", 1, ["タラ", "鱈", "cod"]),
    ("えび", "meat_fish", 1, ["エビ", "海老", "shrimp"]),
    ("いか", "meat_fish", 1, ["イカ", "烏賊", "squid"]),
    ("たこ", "meat_fish", 1, ["タコ", "蛸", "octopus"]),
    ("あさり", "meat_fish", 1, ["アサリ", "clams"]),
    ("しらす", "meat_fish", 1, ["シラス", "whitebait"]),
    ("ちくわ", "meat_fish", 1, ["チクワ", "竹輪", "chikuwa"]),
    ("かまぼこ", "meat_fish", 1, ["カマボコ", "蒲鉾", "kamaboko"]),
    ("ツナ缶", "meat_fish", 1, ["ツナ", "canned tuna"]),
    ("さば缶", "meat_fish", 1, ["サバ缶", "canned mackerel"]),
    # ── dairy & eggs ──
    ("牛乳", "dairy", 1, ["ぎゅうにゅう", "ミルク", "milk"]),
    ("ヨーグルト", "dairy", 1, ["yogurt", "yoghurt"]),
    ("チーズ", "dairy", 1, ["cheese"]),
    ("スライスチーズ", "dairy", 1, ["sliced cheese"]),
    ("バター", "dairy", 1, ["butter"]),
    ("生クリーム", "dairy", 1, ["heavy cream", "cream"]),
    ("卵", "dairy", 1, ["たまご", "タマゴ", "玉子", "eggs", "egg"]),
    ("豆乳", "dairy", 1, ["とうにゅう", "soy milk"]),
    # ── pantry ──
    ("米", "pantry", 1, ["こめ", "お米", "ごはん", "rice"]),
    # pasta types stay distinct (spaghetti/fettuccine ≠ generic pasta) — user 2026-07-12
    ("パスタ", "pantry", 1, ["pasta"]),
    ("そば", "pantry", 1, ["ソバ", "蕎麦", "soba"]),
    ("うどん", "pantry", 1, ["ウドン", "udon"]),
    ("そうめん", "pantry", 1, ["ソウメン", "素麺", "somen"]),
    ("小麦粉", "pantry", 1, ["こむぎこ", "薄力粉", "flour"]),
    ("パン粉", "pantry", 1, ["ぱんこ", "panko", "breadcrumbs"]),
    ("砂糖", "pantry", 1, ["さとう", "sugar"]),
    ("塩", "pantry", 1, ["しお", "salt"]),
    ("醤油", "pantry", 1, ["しょうゆ", "soy sauce"]),
    ("味噌", "pantry", 1, ["みそ", "ミソ", "miso"]),
    ("みりん", "pantry", 1, ["ミリン", "味醂", "mirin"]),
    ("料理酒", "pantry", 1, ["cooking sake"]),
    ("酢", "pantry", 1, ["す", "お酢", "vinegar"]),
    ("サラダ油", "pantry", 1, ["vegetable oil"]),
    ("ごま油", "pantry", 1, ["胡麻油", "sesame oil"]),
    ("オリーブオイル", "pantry", 1, ["olive oil"]),
    ("マヨネーズ", "pantry", 1, ["mayonnaise", "mayo"]),
    ("ケチャップ", "pantry", 1, ["ketchup"]),
    ("中濃ソース", "pantry", 1, ["ソース", "worcestershire sauce"]),
    ("カレールー", "pantry", 1, ["カレー", "curry roux"]),
    ("コンソメ", "pantry", 1, ["consomme", "bouillon"]),
    ("だしの素", "pantry", 1, ["出汁", "だし", "dashi"]),
    ("海苔", "pantry", 1, ["のり", "ノリ", "nori", "seaweed"]),
    ("わかめ", "pantry", 1, ["ワカメ", "若布", "wakame"]),
    ("昆布", "pantry", 1, ["こんぶ", "コンブ", "kombu"]),
    ("かつお節", "pantry", 1, ["鰹節", "おかか", "bonito flakes"]),
    ("ごま", "pantry", 1, ["ゴマ", "胡麻", "sesame"]),
    ("豆腐", "pantry", 1, ["とうふ", "トウフ", "tofu"]),
    ("納豆", "pantry", 1, ["なっとう", "ナットウ", "natto"]),
    ("油揚げ", "pantry", 1, ["あぶらあげ", "aburaage"]),
    ("こんにゃく", "pantry", 1, ["コンニャク", "蒟蒻", "konnyaku"]),
    ("トマト缶", "pantry", 1, ["カットトマト", "canned tomatoes"]),
    ("オートミール", "pantry", 1, ["oatmeal", "oats"]),
    ("はちみつ", "pantry", 1, ["ハチミツ", "蜂蜜", "honey"]),
    ("ジャム", "pantry", 1, ["jam"]),
    ("ピーナッツバター", "pantry", 1, ["peanut butter"]),
    ("ふりかけ", "pantry", 1, ["フリカケ", "furikake"]),
    ("梅干し", "pantry", 1, ["うめぼし", "umeboshi"]),
    ("キムチ", "pantry", 1, ["kimchi"]),
    ("ミックスナッツ", "pantry", 1, ["ナッツ", "nuts"]),
    ("シリアル", "pantry", 1, ["グラノーラ", "cereal", "granola"]),
    # ── bakery ──
    ("食パン", "bakery", 1, ["しょくぱん", "パン", "bread"]),
    ("ロールパン", "bakery", 1, ["rolls"]),
    ("ベーグル", "bakery", 1, ["bagel"]),
    ("クロワッサン", "bakery", 1, ["croissant"]),
    # ── frozen ──
    ("冷凍うどん", "frozen", 1, ["frozen udon"]),
    ("冷凍餃子", "frozen", 1, ["ぎょうざ", "ギョウザ", "gyoza"]),
    ("冷凍ブロッコリー", "frozen", 1, ["frozen broccoli"]),
    ("冷凍ほうれん草", "frozen", 1, ["frozen spinach"]),
    ("冷凍ミックスベジタブル", "frozen", 1, ["mixed vegetables"]),
    ("冷凍ブルーベリー", "frozen", 1, ["frozen blueberries"]),
    ("アイスクリーム", "frozen", 1, ["アイス", "ice cream"]),
    # ── drinks ──
    ("水", "drinks", 1, ["みず", "ミネラルウォーター", "water"]),
    ("炭酸水", "drinks", 1, ["sparkling water"]),
    ("お茶", "drinks", 1, ["緑茶", "green tea", "tea"]),
    ("麦茶", "drinks", 1, ["むぎちゃ", "barley tea"]),
    ("コーヒー", "drinks", 1, ["coffee"]),
    ("紅茶", "drinks", 1, ["こうちゃ", "black tea"]),
    ("オレンジジュース", "drinks", 1, ["orange juice", "juice"]),
    ("ビール", "drinks", 1, ["beer"]),
    ("ワイン", "drinks", 1, ["wine"]),
    # ── household ──
    ("トイレットペーパー", "household", 0, ["toilet paper"]),
    ("ティッシュ", "household", 0, ["tissues"]),
    ("キッチンペーパー", "household", 0, ["paper towels"]),
    ("ラップ", "household", 0, ["サランラップ", "plastic wrap"]),
    ("アルミホイル", "household", 0, ["aluminum foil"]),
    ("食器用洗剤", "household", 0, ["dish soap"]),
    ("洗濯洗剤", "household", 0, ["laundry detergent"]),
    ("柔軟剤", "household", 0, ["fabric softener"]),
    ("ハンドソープ", "household", 0, ["hand soap"]),
    ("シャンプー", "household", 0, ["shampoo"]),
    ("コンディショナー", "household", 0, ["conditioner"]),
    ("ボディソープ", "household", 0, ["body wash"]),
    ("歯磨き粉", "household", 0, ["toothpaste"]),
    ("ゴミ袋", "household", 0, ["trash bags"]),
    ("スポンジ", "household", 0, ["sponge"]),
    ("電池", "household", 0, ["batteries"]),
    ("マスク", "household", 0, ["masks"]),
]


def seed(conn) -> int:
    added = 0
    for name, category, is_edible, aliases in SEED:
        canon = db.canonical(name)
        row = conn.execute(
            "SELECT id, aliases_json, category FROM item_catalog WHERE canonical_name=?",
            (canon,),
        ).fetchone()
        if row:
            # row predates seeding (user-created): backfill curated aliases +
            # category so EN display / search work for it too
            merged = json.loads(row["aliases_json"])
            merged += [a for a in aliases
                       if not any(db.canonical(a) == db.canonical(m) for m in merged)]
            conn.execute(
                "UPDATE item_catalog SET aliases_json=?, "
                "category=COALESCE(category, ?), is_edible=COALESCE(is_edible, ?) "
                "WHERE id=?",
                (json.dumps(merged, ensure_ascii=False), category, is_edible, row["id"]),
            )
            continue
        conn.execute(
            "INSERT INTO item_catalog(canonical_name, display_name, aliases_json, "
            "category, is_edible) VALUES(?,?,?,?,?)",
            (canon, name, json.dumps(aliases, ensure_ascii=False), category, is_edible),
        )
        added += 1
    conn.commit()
    return added


if __name__ == "__main__":
    conn = db.connect()
    n = seed(conn)
    total = conn.execute("SELECT COUNT(*) FROM item_catalog").fetchone()[0]
    print(f"seeded {n} new items ({total} total in catalog)")
