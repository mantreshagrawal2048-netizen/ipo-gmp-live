"""
Live IPO GMP collector (runs automatically on GitHub - you never run it yourself).

What it does every run:
 1. Reads GMP from five public IPO trackers.
 2. Detects sources that merely copy another source (identical quotes) and leaves them out of the average.
 3. Cross-checks the issue price across sources.
 4. Saves a GMP history for every IPO (for trend charts and the back-test).
 5. When an IPO lists, attaches the pre-listing snapshots (at bidding close and just before listing)
    to its record, so the page can compare mean vs median vs each single source against the actual listing gain.
Everything is written to data.json.
"""
import difflib, io, json, re, statistics, sys
from datetime import datetime, timedelta, timezone

import pandas as pd
from playwright.sync_api import sync_playwright

IG_URL = "https://www.investorgain.com/report/ipo-gmp-live/331/"
GY_URL = "https://ipogyani.com/"
IW_URL = "https://ipowatch.in/ipo-grey-market-premium-latest-ipo-gmp/"
IJ_URL = "https://www.ipoji.com/ipo-gmp"
GU_URL = "https://www.ipoguru.in/live-ipo-gmp"
UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/124 Safari/537.36"
IST = timezone(timedelta(hours=5, minutes=30))
NOW = datetime.now(IST)
TODAY = NOW.date()

SOURCE_ORDER = ["InvestorGain", "IPOGyani", "IPOWatch", "IPOJi", "IPOGuru"]  # earlier = higher priority
# These sites show a 0 when they simply have no quote, so a 0 is only trusted if another source quotes a value.
ZERO_UNTRUSTED = {"IPOWatch", "IPOGuru"}
DUP_MIN_PAIRS = 30       # a source is treated as a copy only after this many paired quotes...
DUP_MATCH = 0.95         # ...of which at least this share are identical
POINT_EVERY_H = 3        # save a history point at least this often (or whenever a quote changes)
MAX_POINTS = 200
KEEP_HISTORY_DAYS = 45
MAX_LISTED = 300


# ---------------------------------------------------------------- helpers
def num(text):
    m = re.search(r"-?\d[\d,]*\.?\d*", str(text))
    return float(m.group(0).replace(",", "")) if m else None


def max_num(text):
    """Largest number in a text such as '₹78-82' (the upper price band)."""
    nums = [float(n.replace(",", "")) for n in re.findall(r"\d[\d,]*\.?\d*", str(text))]
    return max(nums) if nums else None


def norm(s):
    t = str(s).lower().replace("&", " ")
    t = re.sub(r"\b(and|limited|ltd|pvt|private)\b", " ", t)
    return re.sub(r"[^a-z0-9]", "", t)


def lookup(d, name):
    """Find an IPO in a {normalised name: {...}} dict, tolerating small spelling differences."""
    key = norm(name)
    if key in d:
        return d[key]
    close = difflib.get_close_matches(key, list(d.keys()), n=1, cutoff=0.88)
    return d[close[0]] if close else None


def parse_date(cell):
    m = re.search(r"(\d{1,2})-([A-Za-z]{3})", str(cell))
    if not m:
        return None
    for year in (TODAY.year, TODAY.year + 1, TODAY.year - 1):
        try:
            d = datetime.strptime(f"{m.group(1)}-{m.group(2)}-{year}", "%d-%b-%Y").date()
        except ValueError:
            continue
        if abs((d - TODAY).days) < 180:
            return d
    return None


def get_html(url, wait_selector):
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(user_agent=UA)
        page.goto(url, wait_until="networkidle", timeout=60000)
        page.wait_for_selector(wait_selector, timeout=30000)
        html = page.content()
        browser.close()
    return html


def find_col(df, prefix):
    for c in df.columns:
        if str(c).strip().lower().startswith(prefix.lower()):
            return c
    return None


# ---------------------------------------------------------------- sources
def fetch_investorgain():
    tables = pd.read_html(io.StringIO(get_html(IG_URL, "table tbody tr")))
    df = next((t for t in tables if find_col(t, "Name") is not None and find_col(t, "GMP") is not None), None)
    if df is None:
        sys.exit("InvestorGain GMP table not found - site layout may have changed")

    c_name, c_gmp, c_price = find_col(df, "Name"), find_col(df, "GMP"), find_col(df, "Price")
    c_lot, c_open, c_close, c_list = find_col(df, "Lot"), find_col(df, "Open"), find_col(df, "Close"), find_col(df, "Listing")

    current, listed = [], []
    for _, r in df.iterrows():
        price = num(r[c_price])
        if price is None or str(r[c_name]).strip() == "Name":
            continue  # repeated header row
        raw = str(r[c_name])
        name = re.split(r"\s+(?:BSE SME|NSE SME|SME|IPO)", raw)[0].strip()
        kind = "SME" if "SME" in raw else "Mainboard"
        gmp_cell = str(r[c_gmp])
        m = re.search(r"₹\s*(-?\d[\d,]*\.?\d*)", gmp_cell)
        gmp = float(m.group(1).replace(",", "")) if m else None
        hl = re.search(r"([\d.]+)\s*↓\s*/\s*([\d.]+)\s*↑", gmp_cell)
        listing = parse_date(r[c_list])

        if listing and listing < TODAY:
            # Already listed: keep final GMP + actual listing gain for the accuracy section.
            # The actual gain appears in brackets in the name cell, e.g. "(5.46%)".
            # An exact 0% is skipped because it can also mean "not captured yet".
            g = re.search(r"\((-?\d+(?:\.\d+)?)%\)", raw)
            if g and gmp is not None and float(g.group(1)) != 0:
                listed.append({"name": name, "type": kind, "price": price, "gmp": gmp,
                               "listing_gain_pct": float(g.group(1)), "listing": listing.isoformat()})
            continue

        d_open, d_close = parse_date(r[c_open]), parse_date(r[c_close])
        current.append({
            "name": name, "type": kind, "price": price, "gmp": gmp,
            "gmp_low": float(hl.group(1)) if hl else None,
            "gmp_high": float(hl.group(2)) if hl else None,
            "lot": int(num(r[c_lot]) or 0),
            "open": d_open.isoformat() if d_open else None,
            "close": d_close.isoformat() if d_close else None,
            "listing": listing.isoformat() if listing else None,
        })
    return current, listed


def fetch_ipogyani():
    """{normalised name: {'gmp':..., 'price':...}}  Failure is not fatal."""
    out = {}
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch()
            page = browser.new_page(user_agent=UA)
            page.goto(GY_URL, wait_until="networkidle", timeout=60000)
            page.wait_for_selector('a[href*="/ipo/"]', timeout=30000)
            cards = page.eval_on_selector_all(
                'a[href*="/ipo/"]', "els => els.map(e => ({href: e.href, text: e.innerText}))")
            browser.close()
        for c in cards:
            s = re.search(r"/ipo/([^/?#]+)", c["href"])
            m = re.search(r"GMP:?\s*[+-]?\d[\d.]*%\s*([+-])\s*Rs\s*([\d,]+\.?\d*)", c["text"])
            if not (s and m):
                continue
            key = norm(re.sub(r"-ipo$", "", s.group(1)))
            if key not in out:
                v = float(m.group(2).replace(",", ""))
                pb = re.search(r"Price Band\s*Rs\s*([\d.,]+)(?:\s*-\s*([\d.,]+))?", c["text"])
                price = float((pb.group(2) or pb.group(1)).replace(",", "")) if pb else None
                out[key] = {"gmp": -v if m.group(1) == "-" else v, "price": price}
    except Exception as e:
        print("IPOGyani skipped:", e)
    return out


def fetch_ipowatch():
    out = {}
    try:
        tables = pd.read_html(io.StringIO(get_html(IW_URL, "table tbody tr")))
        for df in tables:
            c_co, c_g, c_p = find_col(df, "Company"), find_col(df, "GMP"), find_col(df, "Price")
            if c_co is None or c_g is None:
                continue
            for _, r in df.iterrows():
                name = re.sub(r"\s+(Upcoming|Open|Closing Today|Closed|Listed)\b.*$", "", str(r[c_co]).strip(), flags=re.I).strip()
                m = re.search(r"₹\s*(-?\d[\d,]*\.?\d*)", str(r[c_g]))
                if name and m:
                    out[norm(name)] = {"gmp": float(m.group(1).replace(",", "")),
                                       "price": max_num(r[c_p]) if c_p is not None else None}
    except Exception as e:
        print("IPOWatch skipped:", e)
    return out


def fetch_ipoji():
    out = {}
    try:
        tables = pd.read_html(io.StringIO(get_html(IJ_URL, "table tbody tr")))
        for df in tables:
            c_n, c_g, c_p = find_col(df, "IPO"), find_col(df, "GMP"), find_col(df, "Price")
            if c_n is None or c_g is None:
                continue
            for _, r in df.iterrows():
                name = re.split(r"\s+IPO\b", str(r[c_n]).strip())[0].strip()
                m = re.search(r"([+\-\u2212])\s*₹\s*([\d,]+\.?\d*)", str(r[c_g]))
                if name and m:
                    v = float(m.group(2).replace(",", ""))
                    out[norm(name)] = {"gmp": -v if m.group(1) in "-\u2212" else v,
                                       "price": max_num(r[c_p]) if c_p is not None else None}
    except Exception as e:
        print("IPOJi skipped:", e)
    return out


def fetch_ipoguru():
    out = {}
    try:
        tables = pd.read_html(io.StringIO(get_html(GU_URL, "table tbody tr")))
        for df in tables:
            c_n, c_g, c_p = find_col(df, "Company"), find_col(df, "IPO GMP"), find_col(df, "Issue")
            if c_n is None or c_g is None:
                continue
            for _, r in df.iterrows():
                name = re.split(r"\s+(?:Mainboard|SME)\b", str(r[c_n]).strip())[0].strip()
                m = re.search(r"₹\s*(-?\d[\d,]*\.?\d*)", str(r[c_g]))
                if name and m:
                    out[norm(name)] = {"gmp": float(m.group(1).replace(",", "")),
                                       "price": max_num(r[c_p]) if c_p is not None else None}
    except Exception as e:
        print("IPOGuru skipped:", e)
    return out


# ---------------------------------------------------------------- overlap / duplicates
def compute_overlap(history):
    """For every pair of sources: how many paired quotes, and how many were identical."""
    stats = {}
    for h in history.values():
        for p in h.get("points", []):
            s = p.get("src", {})
            for i, a in enumerate(SOURCE_ORDER):
                for b in SOURCE_ORDER[i + 1:]:
                    if a in s and b in s:
                        st = stats.setdefault(f"{a}|{b}", {"n": 0, "exact": 0})
                        st["n"] += 1
                        if abs(s[a] - s[b]) < 0.005:
                            st["exact"] += 1
    return stats


def find_duplicates(overlap):
    """{later source: earlier source it copies}. Needs enough paired quotes before anything is excluded."""
    dup = {}
    for k, st in overlap.items():
        a, b = k.split("|")
        if st["n"] >= DUP_MIN_PAIRS and st["exact"] / st["n"] >= DUP_MATCH and b not in dup:
            dup[b] = a
    return dup


# ---------------------------------------------------------------- history / back-test
def update_history(history, current):
    for x in current:
        if not x["gmp_sources"]:
            continue
        h = history.setdefault(norm(x["name"]), {"points": []})
        h.update({"name": x["name"], "type": x["type"], "price": x["price"],
                  "close": x["close"], "listing": x["listing"]})
        pt = {"t": NOW.isoformat(timespec="minutes"), "avg": x["gmp"], "med": x["gmp_median"], "src": x["gmp_sources"]}
        last = h["points"][-1] if h["points"] else None
        changed = last is None or last["src"] != pt["src"]
        stale = last is None or (NOW - datetime.fromisoformat(last["t"])).total_seconds() >= POINT_EVERY_H * 3600
        if changed or stale:
            h["points"].append(pt)
        h["points"] = h["points"][-MAX_POINTS:]
    cutoff = (TODAY - timedelta(days=KEEP_HISTORY_DAYS)).isoformat()
    for k in [k for k, h in history.items() if h.get("listing") and h["listing"] < cutoff]:
        del history[k]


def make_bt(h, listing_iso):
    """Snapshots to compare against the actual listing gain."""
    pts = h.get("points", [])
    if not pts:
        return None

    def pick(pred):
        c = [p for p in pts if pred(p)]
        return c[-1] if c else None

    final = pick(lambda p: p["t"][:10] < listing_iso) or pts[-1]
    close = pick(lambda p: p["t"][:10] <= h["close"]) if h.get("close") else None
    keep = lambda p: {"t": p["t"], "avg": p["avg"], "med": p.get("med"), "src": p["src"]}
    bt = {"final": keep(final)}
    if close:
        bt["close"] = keep(close)
    return bt


def previous():
    try:
        with open("data.json", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


# ---------------------------------------------------------------- main
def main():
    prev = previous()
    history = prev.get("history", {})

    current, listed_now = fetch_investorgain()
    if not current:
        sys.exit("No IPO rows parsed - keeping the previous data.json")

    fetched = {"IPOGyani": fetch_ipogyani(), "IPOWatch": fetch_ipowatch(),
               "IPOJi": fetch_ipoji(), "IPOGuru": fetch_ipoguru()}

    dups = find_duplicates(compute_overlap(history))

    for x in current:
        raw = {"InvestorGain": x["gmp"]}
        prices = {}
        for name, d in fetched.items():
            rec = lookup(d, x["name"])
            if rec:
                raw[name] = rec["gmp"]
                prices[name] = rec["price"]
        raw = {k: v for k, v in raw.items() if v is not None}

        kept = {}
        for k, v in raw.items():
            if k in ZERO_UNTRUSTED and v == 0:
                # keep a 0 only if some other source really quotes this IPO
                if not any(v2 != 0 or k2 not in ZERO_UNTRUSTED for k2, v2 in raw.items() if k2 != k):
                    continue
            kept[k] = v
        counted = {k: v for k, v in kept.items() if not (k in dups and dups[k] in kept)}

        x["gmp_sources"] = kept
        x["gmp_counted"] = [k for k in SOURCE_ORDER if k in counted]
        x["gmp"] = round(sum(counted.values()) / len(counted), 2) if counted else None
        x["gmp_median"] = round(statistics.median(counted.values()), 2) if counted else None
        x["price_mismatch"] = {k: p for k, p in prices.items() if p is not None and abs(p - x["price"]) > 0.5}

    update_history(history, current)
    overlap = compute_overlap(history)

    # listed IPOs: keep earlier records (incl. their back-test snapshots), add new ones
    merged = {(l["name"], l["listing"]): l for l in prev.get("listed", [])}
    for l in listed_now:
        k = (l["name"], l["listing"])
        if "bt" in merged.get(k, {}):
            l["bt"] = merged[k]["bt"]
        merged[k] = l
    for l in merged.values():
        if "bt" not in l:
            h = history.get(norm(l["name"]))
            bt = make_bt(h, l["listing"]) if h else None
            if bt:
                l["bt"] = bt
    listed = sorted(merged.values(), key=lambda l: l["listing"], reverse=True)[:MAX_LISTED]

    health = {"InvestorGain": len(current)}
    health.update({k: len(v) for k, v in fetched.items()})

    out = {"generated_at": NOW.isoformat(), "source": "InvestorGain, IPOGyani, IPOWatch, IPO Ji, IPO Guru",
           "ipos": current, "listed": listed, "history": history, "overlap": overlap,
           "duplicates": dups, "health": health,
           "dedupe_rule": {"min_pairs": DUP_MIN_PAIRS, "match": DUP_MATCH}}
    with open("data.json", "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, separators=(",", ":"))
    print("Wrote", len(current), "current IPOs |", len(listed), "listed |", "sources:", health,
          "| excluded as duplicates:", dups or "none")


if __name__ == "__main__":
    main()
