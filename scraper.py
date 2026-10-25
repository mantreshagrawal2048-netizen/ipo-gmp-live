"""
Fetches live IPO GMP data from THREE sources (InvestorGain, IPOGyani, IPOWatch),
averages them, and also records listed IPOs (final GMP vs actual listing gain).
Writes data.json. Runs automatically on GitHub - you never run it yourself.
"""
import io, json, re, sys
from datetime import datetime, timedelta, timezone

import pandas as pd
from playwright.sync_api import sync_playwright

IG_URL = "https://www.investorgain.com/report/ipo-gmp-live/331/"
GY_URL = "https://ipogyani.com/"
IW_URL = "https://ipowatch.in/ipo-grey-market-premium-latest-ipo-gmp/"
UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/124 Safari/537.36"
IST = timezone(timedelta(hours=5, minutes=30))
NOW = datetime.now(IST)
TODAY = NOW.date()
MAX_LISTED = 300


def num(text):
    m = re.search(r"-?\d[\d,]*\.?\d*", str(text))
    return float(m.group(0).replace(",", "")) if m else None


def norm(s):
    return re.sub(r"[^a-z0-9]", "", str(s).lower())


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


# ---------------- Source 1: InvestorGain ----------------
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


# ---------------- Source 2: IPOGyani ----------------
def fetch_ipogyani():
    """Returns {normalised IPO name: GMP in rupees}. Failure is not fatal."""
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
                out[key] = -v if m.group(1) == "-" else v
    except Exception as e:  # keep going with one source
        print("IPOGyani skipped:", e)
    return out


# ---------------- Source 3: IPOWatch ----------------
def fetch_ipowatch():
    """Returns {normalised IPO name: GMP in rupees}. Failure is not fatal."""
    out = {}
    try:
        tables = pd.read_html(io.StringIO(get_html(IW_URL, "table tbody tr")))
        for df in tables:
            c_co, c_g = find_col(df, "Company"), find_col(df, "GMP")
            if c_co is None or c_g is None:
                continue
            for _, r in df.iterrows():
                name = re.sub(r"\s+(Upcoming|Open|Closing Today|Closed|Listed)\b.*$", "", str(r[c_co]).strip(), flags=re.I).strip()
                m = re.search(r"₹\s*(-?\d[\d,]*\.?\d*)", str(r[c_g]))
                if name and m:
                    out[norm(name)] = float(m.group(1).replace(",", ""))
    except Exception as e:  # keep going with the other sources
        print("IPOWatch skipped:", e)
    return out


def previous_listed():
    try:
        with open("data.json", encoding="utf-8") as f:
            return json.load(f).get("listed", [])
    except Exception:
        return []


def main():
    current, listed_now = fetch_investorgain()
    gy = fetch_ipogyani()
    iw = fetch_ipowatch()

    for x in current:
        srcs = {}
        if x["gmp"] is not None:
            srcs["InvestorGain"] = x["gmp"]
        g = gy.get(norm(x["name"]))
        if g is not None:
            srcs["IPOGyani"] = g
        w = iw.get(norm(x["name"]))
        # IPOWatch shows 0 when it has no quote. Only trust a 0 if another source also quotes a value.
        if w is not None and (w != 0 or srcs):
            srcs["IPOWatch"] = w
        x["gmp_sources"] = srcs
        x["gmp"] = round(sum(srcs.values()) / len(srcs), 2) if srcs else None

    if not current:
        sys.exit("No IPO rows parsed - keeping the previous data.json")

    merged = {(l["name"], l["listing"]): l for l in previous_listed()}
    for l in listed_now:
        merged[(l["name"], l["listing"])] = l
    listed = sorted(merged.values(), key=lambda l: l["listing"], reverse=True)[:MAX_LISTED]

    with open("data.json", "w", encoding="utf-8") as f:
        json.dump({"generated_at": NOW.isoformat(),
                   "source": "InvestorGain.com + IPOGyani.com + IPOWatch.in",
                   "ipos": current, "listed": listed}, f, ensure_ascii=False, indent=1)
    print(f"Wrote {len(current)} current IPOs (IPOGyani: {len(gy)}, IPOWatch: {len(iw)}) and {len(listed)} listed IPOs")


if __name__ == "__main__":
    main()
