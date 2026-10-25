"""
Fetches live IPO GMP data from InvestorGain and writes data.json.
Runs automatically on GitHub (see .github/workflows/update.yml).
You never need to run this yourself.
"""
import io, json, re, sys
from datetime import datetime, timedelta, timezone

import pandas as pd
from playwright.sync_api import sync_playwright

URL = "https://www.investorgain.com/report/ipo-gmp-live/331/"
IST = timezone(timedelta(hours=5, minutes=30))
NOW = datetime.now(IST)
TODAY = NOW.date()


def num(text):
    m = re.search(r"-?\d[\d,]*\.?\d*", str(text))
    return float(m.group(0).replace(",", "")) if m else None


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


def get_html():
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(user_agent="Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/124 Safari/537.36")
        page.goto(URL, wait_until="networkidle", timeout=60000)
        page.wait_for_selector("table tbody tr", timeout=30000)
        html = page.content()
        browser.close()
    return html


def find_col(df, prefix):
    for c in df.columns:
        if str(c).strip().lower().startswith(prefix.lower()):
            return c
    return None


def main():
    tables = pd.read_html(io.StringIO(get_html()))
    df = next((t for t in tables if find_col(t, "Name") is not None and find_col(t, "GMP") is not None), None)
    if df is None:
        sys.exit("GMP table not found - site layout may have changed")

    c_name, c_gmp, c_price = find_col(df, "Name"), find_col(df, "GMP"), find_col(df, "Price")
    c_lot, c_open, c_close, c_list = find_col(df, "Lot"), find_col(df, "Open"), find_col(df, "Close"), find_col(df, "Listing")

    ipos = []
    for _, r in df.iterrows():
        price = num(r[c_price])
        if price is None or str(r[c_name]).strip() == "Name":
            continue  # repeated header row
        raw = str(r[c_name])
        name = re.split(r"\s+(?:BSE SME|NSE SME|SME|IPO)", raw)[0].strip()
        gmp_cell = str(r[c_gmp])
        m = re.search(r"₹\s*(-?\d[\d,]*\.?\d*)", gmp_cell)
        gmp = float(m.group(1).replace(",", "")) if m else None
        hl = re.search(r"([\d.]+)\s*↓\s*/\s*([\d.]+)\s*↑", gmp_cell)
        listing = parse_date(r[c_list])
        if listing and listing < TODAY:
            continue  # already listed
        d_open, d_close = parse_date(r[c_open]), parse_date(r[c_close])
        ipos.append({
            "name": name,
            "type": "SME" if "SME" in raw else "Mainboard",
            "price": price,
            "gmp": gmp,
            "gmp_low": float(hl.group(1)) if hl else None,
            "gmp_high": float(hl.group(2)) if hl else None,
            "lot": int(num(r[c_lot]) or 0),
            "open": d_open.isoformat() if d_open else None,
            "close": d_close.isoformat() if d_close else None,
            "listing": listing.isoformat() if listing else None,
        })

    if not ipos:
        sys.exit("No IPO rows parsed - keeping the previous data.json")

    with open("data.json", "w", encoding="utf-8") as f:
        json.dump({"generated_at": NOW.isoformat(), "source": "InvestorGain.com", "ipos": ipos}, f, ensure_ascii=False, indent=1)
    print(f"Wrote {len(ipos)} IPOs")


if __name__ == "__main__":
    main()
