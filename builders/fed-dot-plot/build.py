#!/usr/bin/env python3
"""
Fed Dot Plot Animator — data builder.

Scrapes the Federal Reserve's Summary of Economic Projections (SEP) "Figure 2"
dot-plot tables (the per-participant federal-funds-rate projections) straight
from the Fed's own accessible-version HTML pages, overlays the realized
effective federal funds rate from FRED, and writes a single self-contained
data.json next to this script.

The Fed is the single source of truth. Each SEP meeting has a deterministic
accessible-HTML URL:

    https://www.federalreserve.gov/monetarypolicy/fomcprojtabl{YYYYMMDD}.htm

and inside it a "Figure 2" table that lists, for each projection year (and the
longer run), how many participants placed a dot at each 1/8-point rate value.

Meeting dates are discovered automatically:
  * 2021 -> present + scheduled future meetings: scraped from fomccalendars.htm
  * 2012 - 2020: a vetted seed list (history never changes)

Because the URL is deterministic, an unknown/not-yet-published date simply 404s
and is skipped. So when a new SEP is released, the next scheduled run picks it
up with no code change. This is what makes the page "update after every FOMC
meeting" (the four SEP meetings a year — March, June, September, December).

Run:
    FRED_API_KEY=... python build.py
    python build.py --no-fred        # skip the actual-FFR overlay (offline)

Requirements: requests, beautifulsoup4  (see requirements.txt)
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import sys
import time
from pathlib import Path

import requests
from bs4 import BeautifulSoup

HERE = Path(__file__).resolve().parent
REPO = HERE.parent.parent  # builders/fed-dot-plot/ -> repo root
OUT = REPO / "tools" / "fed-dot-plot" / "data.json"

FED = "https://www.federalreserve.gov/monetarypolicy"
CALENDAR_URL = f"{FED}/fomccalendars.htm"
PROJ_URL = FED + "/fomcprojtabl{date}.htm"
# The Fed spelled one meeting's accessible page "projtable" (Mar 2022). Fallback.
PROJ_URL_ALT = FED + "/fomcprojtable{date}.htm"

UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120 Safari/537.36"
)
HEADERS = {"User-Agent": UA}

# SEP / dot-plot meetings 2012-2020 (release date = last day of the meeting).
# The dot plot began January 2012. The March 2020 SEP was cancelled (COVID), so
# 2020 has only three. Wrong dates here are harmless — they 404 and are skipped.
SEED_DATES_2012_2020 = [
    "20120125", "20120425", "20120620", "20120913", "20121212",
    "20130320", "20130619", "20130918", "20131218",
    "20140319", "20140618", "20140917", "20141217",
    "20150318", "20150617", "20150917", "20151216",
    "20160316", "20160615", "20160921", "20161214",
    "20170315", "20170614", "20170920", "20171213",
    "20180321", "20180613", "20180926", "20181219",
    "20190320", "20190619", "20190918", "20191211",
    "20200610", "20200916", "20201216",
]

MONTHS = ["", "Jan", "Feb", "Mar", "Apr", "May", "Jun",
          "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]


def log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


def get(url: str, tries: int = 3) -> requests.Response | None:
    """GET with retries. Returns None on a clean 404, raises after retries."""
    last = None
    for i in range(tries):
        try:
            r = requests.get(url, headers=HEADERS, timeout=30)
            if r.status_code == 404:
                return None
            r.raise_for_status()
            return r
        except requests.RequestException as e:  # noqa: PERF203
            last = e
            time.sleep(1.5 * (i + 1))
    log(f"  ! failed after {tries} tries: {url} ({last})")
    return None


# --------------------------------------------------------------------------- #
# Meeting-date discovery
# --------------------------------------------------------------------------- #
def discover_dates() -> list[str]:
    dates: set[str] = set(SEED_DATES_2012_2020)
    r = get(CALENDAR_URL)
    if r is not None:
        found = re.findall(r"fomcprojtabl(\d{8})\.(?:htm|pdf)", r.text)
        for d in found:
            dates.add(d)
        log(f"discovered {len(set(found))} SEP dates from the FOMC calendar")
    else:
        log("! could not load the FOMC calendar; relying on seed list only")
    return sorted(dates)


# --------------------------------------------------------------------------- #
# Figure 2 parsing
# --------------------------------------------------------------------------- #
RATE_RE = re.compile(r"-?\d+(?:\.\d+)?")


def _text(el) -> str:
    return re.sub(r"\s+", " ", el.get_text(" ", strip=True)).strip()


def _looks_like_rate(s: str) -> float | None:
    """A Figure-2 stub is a single rate value like '3.625' or '0.25%'.
    Reject ranges ('2.0-2.1', used by the GDP/inflation distribution tables)."""
    s = s.replace("–", "-").replace("—", "-").strip()
    if s.count("-") > 1 or re.search(r"\d\s*-\s*\d", s):  # a range, not a level
        return None
    m = RATE_RE.search(s)
    if not m:
        return None
    try:
        return float(m.group(0))
    except ValueError:
        return None


def _label_text(table, soup) -> str:
    """Best-effort heading/caption text describing a table, used to find the
    funds-rate dot table across all historical formats."""
    parts = []
    aria = table.get("aria-labelledby")
    if aria:
        for tid in aria.split():
            el = soup.find(id=tid)
            if el:
                parts.append(_text(el))
    head = table.find_previous(["h2", "h3", "h4", "caption"])
    if head:
        parts.append(_text(head))
    return " | ".join(parts).lower()


def _is_dot_table(label: str) -> bool:
    """True for the funds-rate dot figure; false for Table 1 / Figure 1 /
    the GDP-unemployment-inflation distribution figures (3.A/3.B/3.C)."""
    if any(bad in label for bad in
           ("real gross domestic", "real gdp", "unemployment", "inflation",
            "economic projections")):
        return False
    return ("federal funds rate" in label
            or "target range or target level" in label
            or "policy firming" in label
            or "appropriate monetary policy" in label)


def parse_figure2(html: str):
    """Return (columns, dots) where dots[col] is a list of per-participant
    rate values, or None if no Figure 2 table is found."""
    soup = BeautifulSoup(html, "html.parser")
    chosen = None
    fallback = None
    for table in soup.find_all("table"):
        rows = table.find_all("tr")
        if not rows:
            continue
        # numeric-stub score: count body rows whose first cell is a rate level
        numeric = 0
        for tr in rows[1:]:
            cells = tr.find_all(["th", "td"])
            if cells and _looks_like_rate(_text(cells[0])) is not None:
                numeric += 1
        if numeric < 4:
            continue
        label = _label_text(table, soup)
        if _is_dot_table(label):
            chosen = table
            break
        fallback = fallback or table  # numeric table; keep as a last resort
    table = chosen or fallback
    if table is None:
        return None

    rows = table.find_all("tr")
    # Header: prefer thead's last row, else the first row. Body = everything
    # after the header row(s).
    thead = table.find("thead")
    if thead:
        head_rows = thead.find_all("tr")
        header_cells = head_rows[-1].find_all(["th", "td"])
        body_rows = rows[len(head_rows):]
    else:
        header_cells = rows[0].find_all(["th", "td"])
        body_rows = rows[1:]
    columns = [normalize_col(_text(c)) for c in header_cells][1:]  # drop stub col
    dots = {c: [] for c in columns}
    for tr in body_rows:
        cells = tr.find_all(["th", "td"])
        if not cells:
            continue
        rate = _looks_like_rate(_text(cells[0]))
        if rate is None:
            continue
        for i, cell in enumerate(cells[1:]):
            if i >= len(columns):
                break
            txt = _text(cell).replace("\xa0", "").strip()
            m = re.search(r"\d+", txt)
            count = int(m.group(0)) if m else 0
            dots[columns[i]].extend([rate] * count)
    # Drop empty columns (defensive)
    dots = {c: v for c, v in dots.items() if v}
    columns = [c for c in columns if c in dots]
    if not columns:
        return None
    return columns, dots


def normalize_col(c: str) -> str:
    c = c.strip()
    if re.fullmatch(r"\d{4}", c):
        return c
    if "longer" in c.lower():
        return "Longer run"
    return c


# --------------------------------------------------------------------------- #
# Median + assembly
# --------------------------------------------------------------------------- #
def median(values: list[float]) -> float | None:
    if not values:
        return None
    s = sorted(values)
    n = len(s)
    mid = n // 2
    return s[mid] if n % 2 else round((s[mid - 1] + s[mid]) / 2, 4)


def build_meeting(date8: str, html: str) -> dict | None:
    parsed = parse_figure2(html)
    if not parsed:
        return None
    columns, dots = parsed
    y, m, d = int(date8[:4]), int(date8[4:6]), int(date8[6:8])
    meds = {c: median(v) for c, v in dots.items()}
    n = max((len(v) for v in dots.values()), default=0)
    return {
        "date": f"{y:04d}-{m:02d}-{d:02d}",
        "label": f"{MONTHS[m]} {y}",
        "year": y,
        "columns": columns,
        "dots": dots,
        "median": meds,
        "n": n,
    }


# --------------------------------------------------------------------------- #
# FRED actual federal funds rate
# --------------------------------------------------------------------------- #
def fetch_fred_fedfunds(api_key: str):
    """Return (year_end, current).

    year_end[Y] = realized policy rate at the END of calendar year Y, proxied by
    the FOLLOWING January's effective funds rate. The dots are target-range
    midpoints at year-end; using next-January avoids the mid-December-hike
    blending that drags December's own monthly average ~0.2-0.3pp below the
    actual year-end target (e.g. end-2022 was a 4.375 midpoint, but December's
    average prints ~4.10). Falls back to December of Y if January is missing.
    Only completed years appear. current = the latest monthly observation."""
    url = "https://api.stlouisfed.org/fred/series/observations"
    params = {
        "series_id": "FEDFUNDS",
        "api_key": api_key,
        "file_type": "json",
        "observation_start": "2011-01-01",
    }
    r = requests.get(url, params=params, headers=HEADERS, timeout=30)
    r.raise_for_status()
    by_ym = {}
    for o in r.json().get("observations", []):
        v = o.get("value")
        if v not in (None, ".", ""):
            by_ym[o["date"][:7]] = float(v)
    if not by_ym:
        return {}, None
    year_end = {}
    for ym in by_ym:
        if ym.endswith("-12"):
            y = ym[:4]
            nxt_jan = f"{int(y) + 1}-01"
            year_end[y] = round(by_ym.get(nxt_jan, by_ym[ym]), 3)
    last_ym = max(by_ym)
    current = {"ym": last_ym, "rate": round(by_ym[last_ym], 3)}
    return year_end, current


# --------------------------------------------------------------------------- #
# 1-year-ahead forecast error
# --------------------------------------------------------------------------- #
def attach_errors(meetings: list[dict], year_end: dict):
    """1-year-ahead = the projection for the END OF THE NEXT calendar year.
    error = realized year-end funds rate - projected median. A meeting is
    'realized' only once year_end has its target year (i.e. the year is over)."""
    for mtg in meetings:
        target_year = mtg["year"] + 1
        col = str(target_year)
        proj = mtg["median"].get(col)
        actual = year_end.get(col)
        oya = {"target_year": target_year, "projected_median": proj,
               "actual": None, "error": None}
        if proj is not None and actual is not None:
            oya["actual"] = actual
            oya["error"] = round(actual - proj, 3)
        mtg["one_year_ahead"] = oya


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #
def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-fred", action="store_true",
                    help="skip the FRED actual-FFR overlay")
    args = ap.parse_args()

    dates = discover_dates()
    log(f"fetching {len(dates)} candidate SEP pages ...")
    meetings: list[dict] = []
    skipped: list[str] = []
    for d in dates:
        r = get(PROJ_URL.format(date=d))
        if r is None:
            r = get(PROJ_URL_ALT.format(date=d))  # rare "projtable" spelling
        if r is None:
            skipped.append(d)
            continue
        mtg = build_meeting(d, r.text)
        if mtg is None:
            skipped.append(d + "(no Figure 2)")
            log(f"  - {d}: page exists but no dot table parsed")
            continue
        meetings.append(mtg)
        log(f"  + {mtg['date']}  {mtg['label']:>9}  n={mtg['n']:>2}  "
            f"cols={mtg['columns']}")
        time.sleep(0.4)  # be polite to the Fed's servers

    meetings.sort(key=lambda m: m["date"])
    if not meetings:
        log("FATAL: no meetings parsed")
        return 1

    year_end, current = {}, None
    api_key = os.environ.get("FRED_API_KEY", "").strip()
    if not args.no_fred and api_key:
        try:
            year_end, current = fetch_fred_fedfunds(api_key)
            log(f"FRED FEDFUNDS: realized year-ends {min(year_end)}–"
                f"{max(year_end)}; current {current['rate']}% ({current['ym']})")
        except Exception as e:  # noqa: BLE001
            log(f"! FRED fetch failed ({e}); continuing without overlay")
    elif not args.no_fred:
        log("! FRED_API_KEY not set; continuing without overlay")

    attach_errors(meetings, year_end)

    # most-wrong + accuracy table (realized 1-year-ahead errors only)
    realized = [m for m in meetings if m["one_year_ahead"]["error"] is not None]
    realized_sorted = sorted(realized,
                             key=lambda m: abs(m["one_year_ahead"]["error"]),
                             reverse=True)
    most_wrong = realized_sorted[0]["date"] if realized_sorted else None
    errors_table = [
        {"date": m["date"], "label": m["label"],
         "target_year": m["one_year_ahead"]["target_year"],
         "projected": m["one_year_ahead"]["projected_median"],
         "actual": m["one_year_ahead"]["actual"],
         "error": m["one_year_ahead"]["error"]}
        for m in realized_sorted
    ]

    out = {
        "source": "Federal Reserve, Summary of Economic Projections, "
                  "Figure 2 (accessible HTML). Actual rate: FRED FEDFUNDS.",
        "n_meetings": len(meetings),
        "first_meeting": meetings[0]["date"],
        "latest_meeting": meetings[-1]["date"],
        "most_wrong_date": most_wrong,
        "meetings": meetings,
        "errors_table": errors_table,
        "actual": {"year_end": year_end, "current": current},
    }
    # Idempotent write: only rewrite (and bump the timestamp) when the meaningful
    # content changed, so the scheduled job commits ONLY on a real update — the
    # generated_utc stamp alone must not create a diff on every run.
    changed = True
    if OUT.exists():
        try:
            prev = json.loads(OUT.read_text())
            prev.pop("generated_utc", None)
            changed = prev != out
        except Exception:  # noqa: BLE001
            changed = True
    if not changed:
        log(f"\nno change — {OUT.name} left untouched ({len(meetings)} meetings)")
        return 0
    payload = {"generated_utc": dt.datetime.now(dt.timezone.utc)
                   .strftime("%Y-%m-%dT%H:%M:%SZ"), **out}
    OUT.write_text(json.dumps(payload, separators=(",", ":")) + "\n")
    log(f"\nwrote {OUT.relative_to(REPO)} "
        f"({len(meetings)} meetings, {OUT.stat().st_size//1024} KB)")
    if skipped:
        log(f"skipped {len(skipped)}: {', '.join(skipped)}")
    yrs = sorted({m['year'] for m in meetings})
    log(f"year coverage: {yrs[0]}–{yrs[-1]} "
        f"({len(meetings)} meetings across {len(yrs)} years)")
    if most_wrong:
        mw = next(m for m in meetings if m["date"] == most_wrong)
        oya = mw["one_year_ahead"]
        log(f"most-wrong 1yr-ahead: {mw['label']} projected "
            f"{oya['projected_median']}% for {oya['target_year']}, "
            f"actual {oya['actual']}% (err {oya['error']:+}pp)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
