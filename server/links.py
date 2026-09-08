"""
links.py — reading a pasted link. No network: a URL in, what it names out.

When OpenStreetMap has never heard of a shop — the Montgomery Whole Foods
opened after the map was last drawn — the household still has two things
that name it exactly: the pin they can share from Google Maps, and the chain's
own store page. Either is a better identity than a typed name, because a
person chose it while looking at the right shop.

Three shapes are read here; branches.py turns them into a store row.

  - A chain's store page: `wholefoodsmarket.com/stores/<slug>`,
    `wegmans.com/stores/<slug>`, `shoprite.com/…/rsid/<n>/…`. Exact — the
    URL is the branch.
  - A Google Maps place link. The long form carries the place name in the path
    and two coordinate pairs: `@lat,lon` is the VIEWPORT centre, `!3d<lat>!4d
    <lon>` in the data blob is the pin itself, and the pin wins. `maps?q=lat,lon`
    and `/maps/search/<q>/@lat,lon` are the other shapes people paste.
  - A Google short link (`maps.app.goo.gl`, `goo.gl/maps`): only a redirect,
    which lookup.py follows before this is asked again.

Everything else is refused, and so is a link that looks like Google Maps but
carries no coordinates — a place name alone is a search, not an identity.
"""

import re
from urllib.parse import parse_qs, unquote_plus, urlparse

SHORT_HOSTS = ("maps.app.goo.gl", "goo.gl", "g.co")
_LATLON = r"(-?\d{1,2}(?:\.\d+)?),\s*(-?\d{1,3}(?:\.\d+)?)"


def parse_link(url: str) -> dict | None:
    """One of {"kind": "chain", …}, {"kind": "maps", …}, {"kind": "short", "url"},
    or None. Never a guess: a Google link without coordinates is None."""
    url = (url or "").strip()
    if not re.match(r"https?://", url, re.I):
        return None
    try:
        u = urlparse(url)
    except ValueError:
        return None
    host = (u.hostname or "").lower()
    path = u.path or ""

    if host.endswith(SHORT_HOSTS):
        return {"kind": "short", "url": url}

    # Google's cookie interstitial wraps the real link in ?continue=.
    if host.startswith("consent.google."):
        inner = parse_qs(u.query).get("continue", [""])[0]
        return parse_link(unquote_plus(inner)) if inner else None

    if host.endswith("wholefoodsmarket.com"):
        m = re.match(r"^/stores/([a-z0-9-]+)/?$", path, re.I)
        return {"kind": "chain", "chain": "wholefoods", "slug": m.group(1).lower()} if m else None
    if host.endswith("wegmans.com"):
        m = re.match(r"^/stores/([a-z0-9-]+)/?$", path, re.I)
        return {"kind": "chain", "chain": "wegmans", "slug": m.group(1).lower()} if m else None
    if host.endswith("shoprite.com"):
        m = re.search(r"/rsid/(\d+)(?:/|$)", path)
        return {"kind": "chain", "chain": "shoprite", "rsid": m.group(1)} if m else None

    if "google." in host or host == "maps.google.com":
        return _maps(u.path or "", u.query or "")
    return None


def _maps(path: str, query: str) -> dict | None:
    lat = lon = None
    # The pin itself, from the data blob. Present on shared place links.
    m = re.search(r"!3d" + _LATLON.replace(r",\s*", "!4d"), path)
    if m:
        lat, lon = float(m.group(1)), float(m.group(2))
    if lat is None:
        m = re.search(r"/@" + _LATLON, path)
        if m:
            lat, lon = float(m.group(1)), float(m.group(2))
    if lat is None:
        q = parse_qs(query)
        for key in ("q", "ll", "query"):
            m = re.match(r"^\s*" + _LATLON + r"\s*$", (q.get(key) or [""])[0])
            if m:
                lat, lon = float(m.group(1)), float(m.group(2))
                break
    if lat is None or lon is None or not (-90 <= lat <= 90 and -180 <= lon <= 180):
        return None
    name = ""
    m = re.search(r"/maps/(?:place|search)/([^/@]+)", path)
    if m:
        name = unquote_plus(m.group(1)).strip()
        if re.match(r"^\s*" + _LATLON + r"\s*$", name):
            name = ""          # "/place/40.4,-74.6" is a coordinate, not a name
    return {"kind": "maps", "name": name, "lat": lat, "lon": lon}


def town_candidates(town: str) -> list[str]:
    """'montgomery-township' -> ['montgomery-township', 'montgomery']. A chain
    keys its page by the town people say, and OpenStreetMap by the municipality;
    both are tried, and the caller verifies whichever page answers against the
    pin, so a wrong one is refused rather than believed."""
    out = [town]
    bare = re.sub(r"-(township|twp|borough|boro|city|village)$", "", town)
    if bare and bare != town:
        out.append(bare)
    return out
