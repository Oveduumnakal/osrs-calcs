#!/usr/bin/env python3
"""Rebuild the Hop & Shop tab's shop snapshot (the `const DATA = ...;` line in index.html) from the OSRS Wiki.

Usage: python3 tools/hop-shop/build_shop_data.py [index.html]

Keeps every coin shop line whose item can be sold on the GE when the item stacks or the shop is within
NEAR_MAX tiles of a bank. GE prices are not stored: the tab prices everything live.
Sources: wiki Bucket API (storeline, infobox_shop, map, locline), shop/bank page wikitext ({{Map}} pins),
Category:Stackable items, and prices.runescape.wiki /mapping for item ids and store values.
"""
import collections, datetime, html, json, re, sys, time, urllib.parse, urllib.request

UA = "osrs-calcs hop-shop builder (https://github.com/oveduumnakal/osrs-calcs)"
WIKI = "https://oldschool.runescape.wiki/api.php"
MAPPING = "https://prices.runescape.wiki/api/v1/osrs/mapping"
NEAR_MAX = 60          # tiles; the tab's "near bank" slider tops out here
FLOOR_TILES = 10       # walking penalty for a bank on another floor
SKIP_BANK = ("Tutorial Island", "Clan Hall", "Bank boat", "Banker (Bank boat)", "Bank chest (amenity)")
BAD_SHOP = re.compile(r"Rewards|Mixology", re.I)            # points shops mislabeled as coins
MODE_ONLY = re.compile(r"League|Deadman|Grid Master", re.I)  # stock that only exists on special worlds


def get(url, params=None):
    if params:
        url += "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=90) as r:
        return json.load(r)


def lua(s):
    return "'" + s.replace("\\", "\\\\").replace("'", "\\'") + "'"


def bucket(name, fields, where=None, page=500):
    """All rows of a Bucket table, paged."""
    rows, off = [], 0
    while True:
        q = f"bucket('{name}').select({','.join(repr(f) for f in fields)})"
        if where:
            q += f".where({where})"
        q += f".limit({page}).offset({off}).run()"
        d = get(WIKI, {"action": "bucket", "format": "json", "query": q})
        if "error" in d:
            raise RuntimeError(d["error"])
        got = d.get("bucket", [])
        rows += got
        if len(got) < page:
            return rows
        off += page
        time.sleep(0.4)


def bucket_pages(name, fields, pages, batch=25):
    rows = []
    for i in range(0, len(pages), batch):
        cond = "bucket.Or(" + ",".join("{'page_name'," + lua(p) + "}" for p in pages[i:i + batch]) + ")"
        rows += bucket(name, fields, cond, page=5000)
        time.sleep(0.3)
    return rows


def wikitext(titles):
    out = {}
    for i in range(0, len(titles), 20):
        d = get(WIKI, {"action": "query", "prop": "revisions", "rvprop": "content", "rvslots": "main",
                       "titles": "|".join(titles[i:i + 20]), "format": "json", "formatversion": 2, "redirects": 1})
        q = d["query"]
        norm = {n["to"]: n["from"] for n in q.get("normalized", [])}
        redir = {r["to"]: r["from"] for r in q.get("redirects", [])}
        for p in q["pages"]:
            revs = p.get("revisions") or []
            t = redir.get(p["title"], p["title"])
            out[norm.get(t, t)] = revs[0]["slots"]["main"]["content"] if revs else ""
        time.sleep(0.4)
    return out


def category(title):
    out, cont = [], {}
    while True:
        p = {"action": "query", "list": "categorymembers", "cmtitle": title, "cmlimit": 500, "cmnamespace": 0, "format": "json"}
        p.update(cont)
        d = get(WIKI, p)
        out += [m["title"] for m in d["query"]["categorymembers"]]
        if "continue" not in d:
            return out
        cont = d["continue"]
        time.sleep(0.3)


def clean(s):
    s = re.sub(r"\[\[([^|\]]*\|)?([^\]]*)\]\]", r"\2", str(s or ""))
    s = re.sub(r"<[^>]+>", "", s)
    s = re.sub(r"\{\{[^}]*\}\}", "", s)
    s = html.unescape(s)
    s = re.sub(r"\[UK\]\s*\d+(?:st|nd|rd|th)\s+floor\s*\[US\]", "", s)
    return re.sub(r"\s+", " ", s).strip()


def map_templates(text):
    out, i = [], 0
    while True:
        i = text.find("{{Map", i)
        if i < 0:
            return out
        depth, j = 0, i
        while j < len(text):
            if text.startswith("{{", j):
                depth += 1
                j += 2
            elif text.startswith("}}", j):
                depth -= 1
                j += 2
                if depth == 0:
                    break
            else:
                j += 1
        out.append(text[i + 5:j - 2])
        i = j


def parse_map(body):
    """{{Map}} pins as (x, y, plane, mapID): named x=/y=, "x:1,y:2", or positional "x,y" pairs (polygons -> centroid)."""
    pts, pairs = [], []
    nx = ny = None
    plane = mapid = 0
    mtype = ""
    for tok in re.split(r"[|\n]", body):
        t = tok.strip()
        m = re.match(r"^x\s*[:=]\s*(-?\d+)\s*,\s*y\s*[:=]\s*(-?\d+)", t, re.I)
        if m:
            pts.append((int(m.group(1)), int(m.group(2))))
            continue
        m = re.match(r"^(-?\d+)\s*[,:]\s*(-?\d+)$", t)
        if m:
            pairs.append((int(m.group(1)), int(m.group(2))))
            continue
        m = re.match(r"^(\w+)\s*=\s*(-?\d+|\S.*)$", t)
        if m:
            k, v = m.group(1).lower(), m.group(2).strip()
            num = re.match(r"^-?\d+$", v)
            if k == "x" and num:
                nx = int(v)
            elif k == "y" and num:
                ny = int(v)
            elif k == "plane" and num:
                plane = int(v)
            elif k == "mapid" and num:
                mapid = int(v)
            elif k == "mtype":
                mtype = v.lower()
    if nx is not None and ny is not None:
        pts.append((nx, ny))
    if pairs:
        if any(s in mtype for s in ("polygon", "line", "rectangle", "square")):
            pts.append((sum(p[0] for p in pairs) / len(pairs), sum(p[1] for p in pairs) / len(pairs)))
        else:
            pts += pairs
    return [(x, y, plane, mapid) for x, y in pts]


def feature_points(row):
    """Shop pins from a Bucket map row: GeoJSON features (polygons -> centroid), else the map's centre."""
    pts = []
    try:
        feats = json.loads(row.get("features") or "[]")
    except ValueError:
        feats = []
    for f in feats if isinstance(feats, list) else []:
        g, pr = f.get("geometry") or {}, f.get("properties") or {}
        flat = []

        def walk(c):
            if isinstance(c, list) and len(c) >= 2 and all(isinstance(v, (int, float)) for v in c[:2]):
                flat.append((c[0], c[1]))
            elif isinstance(c, list):
                for cc in c:
                    walk(cc)
        walk(g.get("coordinates"))
        if not flat:
            continue
        p, mid = int(pr.get("plane") or 0), int(pr.get("mapID") or 0)
        if g.get("type") in ("Polygon", "LineString", "MultiPolygon"):
            pts.append((sum(a for a, _ in flat) / len(flat), sum(b for _, b in flat) / len(flat), p, mid))
        else:
            pts += [(a, b, p, mid) for a, b in flat]
    if not pts:
        try:
            o = json.loads(row.get("options") or "{}")
            if "x" in o and "y" in o:
                pts.append((o["x"], o["y"], int(o.get("plane") or 0), int(o.get("mapID") or 0)))
        except ValueError:
            pass
    return pts


def num(x):
    try:
        return float(str(x).replace(",", ""))
    except ValueError:
        return None


def main():
    index = sys.argv[1] if len(sys.argv) > 1 else "index.html"
    print("fetching shop lines…")
    lines = bucket("storeline", ["page_name", "sold_item", "store_sell_price", "store_currency", "store_delta", "store_stock",
                                 "store_sell_multiplier", "restock_time", "store_notes", "sold_item_json"], "'store_currency','Coins'")
    locs = collections.defaultdict(list)   # multi-site shops (e.g. sawmills) have one infobox row per site
    for r in bucket("infobox_shop", ["page_name", "location"]):
        loc = clean(r.get("location"))
        if loc and loc not in locs[r["page_name"]]:
            locs[r["page_name"]].append(loc)
    shop_pages = sorted({r["page_name"] for r in lines})
    print(f"  {len(lines)} lines in {len(shop_pages)} shops; fetching coordinates…")
    shop_pts = collections.defaultdict(list)
    for r in bucket_pages("map", ["page_name", "features", "options"], shop_pages):
        shop_pts[r["page_name"]] += feature_points(r)
    for page, text in wikitext(shop_pages).items():
        for body in map_templates(text):
            shop_pts[page] += parse_map(body)
    pre = get(WIKI, {"action": "query", "list": "prefixsearch", "pssearch": "Banker (", "pslimit": 100, "format": "json"})
    bank_pages = ["Bank booth", "Bank chest", "Bank Deposit Box", "Deposit Box", "Bank Chest-wreck", "Bank chest (The Great Conch)", "Banker", "Banker tutor"]
    bank_pages += [p["title"] for p in pre["query"]["prefixsearch"]
                   if p["title"].startswith("Banker (") and "Robbery" not in p["title"] and "disambiguation" not in p["title"]]
    banks = []
    for r in bucket_pages("locline", ["page_name", "coordinates", "plane", "mapid"], ["Bank booth", "Bank chest"]):
        kind = "chest" if "chest" in r["page_name"].lower() else "booth"
        for c in r.get("coordinates") or []:
            m = re.match(r"x:(-?\d+),y:(-?\d+)", c)
            if m:
                banks.append((int(m.group(1)), int(m.group(2)), int(r.get("plane") or 0), int(r.get("mapid") or 0), kind))
    for page, text in wikitext([p for p in bank_pages if p not in ("Bank booth", "Bank chest")]).items():
        if any(s in page for s in SKIP_BANK):
            continue
        kind = "deposit" if "deposit" in page.lower() else ("chest" if "chest" in page.lower() else "banker")
        banks += [(x, y, p, m, kind) for body in map_templates(text) for x, y, p, m in parse_map(body)]
    print(f"  {len(banks)} bank pins; fetching stackable items and GE ids…")
    stackable = {t.lower() for t in category("Category:Stackable items")}
    mapping = {m["name"].lower(): m for m in get(MAPPING)}

    def nearest(pt, kinds):
        x, y, p, m = pt
        best = None
        for bx, by, bp, bm, bk in banks:
            if bk in kinds and bm == m:
                d = max(abs(bx - x), abs(by - y)) + FLOOR_TILES * abs(bp - p)
                if best is None or d < best[0]:
                    best = (d, bk, bp != p)
        return best

    shops = {}
    for page in shop_pages:
        bank = dep = None
        for pt in shop_pts.get(page, []):
            b = nearest(pt, ("booth", "chest", "banker"))
            if b and (bank is None or b[0] < bank[0]):
                bank = b
            d = nearest(pt, ("deposit",))
            if d and (dep is None or d[0] < dep[0]):
                dep = d
        ls = locs.get(page, [])
        loc = " / ".join(ls) if len(ls) <= 4 else " / ".join(ls[:3]) + f" +{len(ls) - 3} more"
        shops[page] = {"loc": loc, "bank": bank, "dep": dep}

    out, used, items, seen = [], {}, {}, {}
    for r in lines:
        page, name = r["page_name"], (r.get("sold_item") or "").strip()
        note = clean(r.get("store_notes"))
        m = mapping.get(name.lower())
        price, stock_s = num(r.get("store_sell_price")), str(r.get("store_stock") or "").strip()
        stock = -1 if stock_s in ("∞", "inf", "Infinite") else num(stock_s)
        if BAD_SHOP.search(page) or MODE_ONLY.search(note) or not m or not price or price <= 0 or not stock:
            continue
        sh = shops[page]
        near = any(b is not None and round(b[0]) <= NEAR_MAX for b in (sh["bank"], sh["dep"]))
        st = name.lower() in stackable
        if not (st or near):
            continue
        try:
            lj = json.loads(r.get("sold_item_json") or "{}")
        except ValueError:
            lj = {}
        restock = num(r.get("restock_time"))
        line = [used.setdefault(page, len(used)), m["id"], int(stock), int(price), int(num(r.get("store_sell_multiplier")) or 1000),
                int(num(r.get("store_delta")) or 0), int(restock) if restock else 0, 1 if st else 0, note,
                1 if str(lj.get("Members", "Yes")).lower().startswith("y") else 0]
        key = tuple(line[:7])
        if key in seen:   # the wiki repeats some lines across pre/post-quest tables; keep the unconditional one
            if seen[key][8] and not note:
                seen[key][:] = line
            continue
        seen[key] = line
        out.append(line)
        items[m["id"]] = [m["name"], m.get("value") or 0, 1 if m.get("members") else 0]
    shop_list = [None] * len(used)
    for page, i in used.items():
        s = shops[page]
        mems = [ln[9] for ln in out if ln[0] == i]
        shop_list[i] = [page, s["loc"], 1 if mems and min(mems) == 1 else 0,
                        round(s["bank"][0]) if s["bank"] else None, s["bank"][1] if s["bank"] else None,
                        1 if s["bank"] and s["bank"][2] else 0, round(s["dep"][0]) if s["dep"] else None]
    data = {"v": datetime.date.today().isoformat(), "shops": shop_list, "items": items, "lines": out}
    blob = json.dumps(data, separators=(",", ":"), ensure_ascii=False).replace("</", "<\\/")
    src = open(index, encoding="utf-8").read()
    new, n = re.subn(r'^const DATA = \{"v":.*\};$', lambda _: "const DATA = " + blob + ";", src, count=1, flags=re.M)
    if n != 1:
        sys.exit("could not find the Hop & Shop `const DATA = {...};` line in " + index)
    open(index, "w", encoding="utf-8").write(new)
    print(f"wrote {len(out)} lines / {len(shop_list)} shops / {len(items)} items ({len(blob):,} bytes) into {index}")


if __name__ == "__main__":
    main()
