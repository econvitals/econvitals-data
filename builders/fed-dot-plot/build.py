#!/usr/bin/env python3
"""
Fed Dot Plot Animator — data builder.

Scrapes the Federal Reserve's Summary of Economic Projections (SEP) "Figure 2"
dot-plot tables (the per-participant federal-funds-rate projections) straight
from the Fed's own accessible-version HTML pages, overlays the realized rate —
the year-end FOMC target from the Board's openmarket.htm and the current H.15
effective federal funds rate, FRED only as the fallback when either Board
source fails — and writes a single self-contained data.json next to this script.

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
    python build.py                  # FRED_API_KEY=... only arms the FRED fallback
    python build.py --no-actual      # skip the realized-rate overlay (offline)

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


# requests puts the full keyed URL into its exception text; the log is a public Actions log.
# GitHub masks the exact secret, but a log line should not carry it in the first place.
_KEYED = re.compile(r"(api_key=)[^&\s'\"]+")


def _scrub(text: str) -> str:
    """`text` with any FRED key value replaced by <redacted>."""
    return _KEYED.sub(r"\1<redacted>", text)

HERE = Path(__file__).resolve().parent
REPO = HERE.parent.parent  # builders/fed-dot-plot/ -> repo root
OUT = REPO / "tools" / "fed-dot-plot" / "data.json"

FED = "https://www.federalreserve.gov/monetarypolicy"
CALENDAR_URL = f"{FED}/fomccalendars.htm"
PROJ_URL = FED + "/fomcprojtabl{date}.htm"
# The Fed spelled one meeting's accessible page "projtable" (Mar 2022). Fallback.
PROJ_URL_ALT = FED + "/fomcprojtable{date}.htm"
# Before the Fed gave the projections their own page, the accessible tables rode on the
# MINUTES as an appendix: fomcminutes<date>epa.htm ("economic projections, accessible").
# December 2012 exists ONLY there — no fomcprojtabl page was ever published for it, which
# is why that meeting was missing from this dataset until 2026-09-16.
PROJ_URL_EPA = FED + "/fomcminutes{date}epa.htm"

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


# In the 2012-2014 releases Figure 2's stub is a RANGE ("0 - 0.37", "0.38 - 0.62") rather
# than a single rate. Each range brackets exactly one quarter point — 0.38-0.62 is 0.50,
# 0.63-0.87 is 0.75 — because the pre-September-2014 SEP asked for "the target federal funds
# rate at year-end", which moved on a quarter-point grid. The one ambiguous band is the first,
# "0 - 0.37", which contains both 0 and 0.25; the era's dots never use 0 (the lowest dot in
# every SEP from January 2012 to June 2014 is 0.25), so it resolves to 0.25.
#
# Verified before use: reconstructing Sep 2012, Mar 2013 and Dec 2013 from their range tables
# reproduces this builder's own exact-value parse, participant for participant, in every
# column (2026-09-16).
def _rate_from_range(stub: str):
    nums = [float(x) for x in RATE_RE.findall(stub)]
    if len(nums) < 2:
        return None
    lo, hi = nums[0], nums[1]
    hits = [round(q * 0.25, 3) for q in range(1, 80) if lo - 1e-9 <= q * 0.25 <= hi + 1e-9]
    return hits[0] if len(hits) == 1 else (hits[-1] if hits else None)


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
            stub = _text(cells[0]) if cells else ""
            if cells and (_looks_like_rate(stub) is not None
                          or _rate_from_range(stub) is not None):
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
        stub = _text(cells[0])
        rate = _looks_like_rate(stub)
        if rate is None:
            rate = _rate_from_range(stub)     # 2012-2014 releases band the stub
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
# The realized rate: the Federal Reserve Board's own pages, FRED only as fallback
# --------------------------------------------------------------------------- #
# Two Board sources, both read with plain HTTP (the Action has no macrolib; these mirror its
# `frb:FOMC/…` and `frb:H15/…` readers):
#   * the FOMC's target, openmarket.htm — one table per year of CHANGES ONLY, 2003 on (the
#     1990-2002 archive is not needed: the year-ends scored here start in 2008), each row dated
#     by the day the new target or range TOOK EFFECT. Checked 2026-11-15 against FRED's
#     DFEDTARU/DFEDTARL over 2008-12-16..2026-11-15: identical on 6,542 of 6,544 calendar days;
#     FRED dates the first two hikes of the range era by the announcement (2015-12-16,
#     2016-12-14), the Board by the effective day — no December 31 is affected;
#   * the H.15 effective federal funds rate, RIFSPFF_N.B (daily, business days) and
#     RIFSPFF_N.M (monthly), from the release's own XML package — FRED's DFF and FEDFUNDS are
#     relays of the same numbers.
# FRED (FRED_API_KEY) is read only when a Board source fails, and `source` then says so.
FOMC_TARGET_URL = f"{FED}/openmarket.htm"
H15_XML_URL = "https://www.federalreserve.gov/releases/h15/data/FRB_H15_xml.zip"
_FOMC_HEADING = "target federal funds rate or range"
_FOMC_YEAR = re.compile(r"<h4>\s*(\d{4})\s*</h4>(.*?)</table>", re.S)
_FOMC_ROW = re.compile(r"<tr\b[^>]*>(.*?)</tr>", re.S)     # NOT `<tr>`: half the rows are <tr class="alt">
_FOMC_CELL = re.compile(r"<t([dh])\b[^>]*>(.*?)</t[dh]>", re.S)
_FOMC_LEVEL = re.compile(r"^(\d+(?:\.\d+)?)(?:-(\d+(?:\.\d+)?))?$")


def _cell_text(s: str) -> str:
    return re.sub(r"\s+", " ", BeautifulSoup(s, "html.parser").get_text()).replace("\xa0", " ").strip()


def _fomc_bp(cell: str):
    """'25' -> (25, 25); '75-100' -> (75, 100) (December 2008); '...' / '0' -> None."""
    c = cell.strip()
    if c in ("", "...", "…", "0", "—", "-"):
        return None
    m = re.match(r"^(\d+)(?:\s*-\s*(\d+))?$", c)
    if not m:
        raise ValueError(f"FOMC target page: change cell {cell!r} is not basis points")
    a = int(m.group(1))
    return (a, int(m.group(2)) if m.group(2) else a)


def parse_fomc_target(page: str) -> list[tuple[str, float, float]]:
    """[(effective ISO date, lower, upper)] ascending, off the Board's openmarket.htm; a single
    target (before 2008-12-16) has lower == upper. The Board's own identity is checked on every
    row — its change column must equal the difference between its level and the row before it —
    so a dropped or misread row raises instead of bending the step series. Pure, for the test."""
    if len(page) < 2048 or _FOMC_HEADING not in page:
        raise ValueError(f"FOMC target page answered ({len(page):,} chars) without its table "
                         f"heading {_FOMC_HEADING!r}; refused")
    rows = []
    for ym in _FOMC_YEAR.finditer(page):
        year = int(ym.group(1))
        for rm in _FOMC_ROW.finditer(ym.group(2)):
            cells = _FOMC_CELL.findall(rm.group(1))
            if not cells or cells[0][0] == "h":
                continue
            vals = [_cell_text(c) for _, c in cells]
            if len(vals) != 4:
                raise ValueError(f"FOMC target page: a {year} row has {len(vals)} cells ({vals})")
            day = vals[0].rstrip("*† ").strip()
            d = None
            for fmt in ("%B %d %Y", "%b %d %Y", "%b. %d %Y"):
                try:
                    d = dt.datetime.strptime(f"{day} {year}", fmt).date()
                    break
                except ValueError:
                    continue
            if d is None:
                raise ValueError(f"FOMC target page: cannot read the date {vals[0]!r} under {year}")
            lm = _FOMC_LEVEL.match(vals[3].replace(" ", ""))
            if not lm:
                raise ValueError(f"FOMC target page: the level on {d} reads {vals[3]!r}")
            lo = float(lm.group(1))
            hi = float(lm.group(2)) if lm.group(2) else lo
            if not (0.0 <= lo <= hi <= 25.0) or hi - lo > 1.0:
                raise ValueError(f"FOMC target page: the level on {d} reads {vals[3]!r}")
            rows.append((d, lo, hi, _fomc_bp(vals[1]), _fomc_bp(vals[2])))
    rows.sort(key=lambda r: r[0])
    if len(rows) < 20:
        raise ValueError(f"FOMC target page parsed to {len(rows)} rows; refused as a short history")
    if len({r[0] for r in rows}) != len(rows):
        raise ValueError("FOMC target page: two rows on the same date")
    for prev, cur in zip(rows, rows[1:]):
        up, down = cur[3], cur[4]
        if (up is None) == (down is None):
            raise ValueError(f"FOMC target page: the {cur[0]} row is neither an increase nor a decrease")
        sign, (a, b) = (1, up) if up is not None else (-1, down)
        if {round((cur[1] - prev[1]) * 100), round((cur[2] - prev[2]) * 100)} != {sign * a, sign * b}:
            raise ValueError(f"FOMC target page: the level chain breaks at {cur[0]} (row before: "
                             f"{prev[0]} {prev[1]:g}-{prev[2]:g}); a change is missing or misread")
    return [(r[0].isoformat(), r[1], r[2]) for r in rows]


def _eastern_today() -> dt.date:
    try:
        from zoneinfo import ZoneInfo
        return dt.datetime.now(ZoneInfo("America/New_York")).date()
    except Exception:  # noqa: BLE001 — no tz database: EST is never more than an hour off
        return (dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=5)).date()


def target_dailies(rows, through: str):
    """(upper, lower, single) as {iso: value}, one key per CALENDAR day like FRED's series:
    the range from RANGE_FROM through `through`, the single target from 2008-01-01 (what the
    FRED read asked for; no earlier year-end is scored) to the day before the range began."""
    upper, lower, single = {}, {}, {}
    i = 0
    d, last = dt.date(2008, 1, 1), dt.date.fromisoformat(through)
    if rows[0][0] > d.isoformat():
        raise ValueError(f"FOMC target page starts {rows[0][0]}, after 2008-01-01")
    while d <= last:
        iso = d.isoformat()
        while i + 1 < len(rows) and rows[i + 1][0] <= iso:
            i += 1
        _, lo, hi = rows[i]
        if iso >= RANGE_FROM:
            upper[iso], lower[iso] = hi, lo
        else:
            single[iso] = lo
        d += dt.timedelta(days=1)
    return upper, lower, single


def board_targets():
    """(upper, lower, single) from the Board's openmarket.htm, through today (Eastern) — or
    through the newest row, when a decision afternoon has posted tomorrow's effective range."""
    r = get(FOMC_TARGET_URL)
    if r is None:
        raise RuntimeError(f"HTTP 404 at {FOMC_TARGET_URL}")
    rows = parse_fomc_target(r.text)
    return target_dailies(rows, max(_eastern_today().isoformat(), rows[-1][0]))


_H15_SERIES = re.compile(r'<kf:Series\b[^>]*SERIES_NAME="(RIFSPFF_N\.[BM])"[^>]*>(.*?)</kf:Series>', re.S)
_H15_OBS = re.compile(r'<frb:Obs\b[^>]*?OBS_STATUS="([^"]*)"[^>]*?OBS_VALUE="([^"]*)"'
                      r'[^>]*?TIME_PERIOD="(\d{4}-\d{2}-\d{2})"')


def parse_h15_fedfunds(xml_text: str) -> tuple[dict, dict]:
    """(daily, monthly) {iso: value} for the effective federal funds rate off H15_data.xml —
    RIFSPFF_N.B (business days) and RIFSPFF_N.M (monthly averages, which the package dates
    by the LAST day of the month). A day with no rate is written OBS_STATUS="ND" with
    OBS_VALUE="-9999" (680 of them in RIFSPFF_N.B — the holidays): only status "A" is a
    value, or the "Current" line could read -9999. Pure, for the test."""
    out = {}
    for m in _H15_SERIES.finditer(xml_text):
        obs = {}
        for status, v, d in _H15_OBS.findall(m.group(2)):
            if status != "A":
                continue
            try:
                x = float(v)
            except ValueError:
                continue
            if x > -1000:                       # belt and braces against the -9999 sentinel
                obs[d] = x
        out[m.group(1)] = obs
    daily, monthly = out.get("RIFSPFF_N.B") or {}, out.get("RIFSPFF_N.M") or {}
    if len(daily) < 1000 or len(monthly) < 100:
        raise ValueError(f"H.15 package: RIFSPFF_N.B {len(daily)} days, RIFSPFF_N.M "
                         f"{len(monthly)} months — not the release; refused")
    return daily, monthly


def board_h15_fedfunds() -> tuple[dict, dict]:
    import io
    import zipfile
    r = requests.get(H15_XML_URL, headers=HEADERS, timeout=120)
    r.raise_for_status()
    if r.content[:2] != b"PK":
        raise ValueError(f"H.15 package is not a ZIP (starts {r.content[:40]!r})")
    with zipfile.ZipFile(io.BytesIO(r.content)) as z:
        name = next((n for n in z.namelist() if n.lower() == "h15_data.xml"), None)
        if name is None:
            raise ValueError(f"H.15 package has no H15_data.xml ({z.namelist()})")
        return parse_h15_fedfunds(z.read(name).decode("utf-8", "replace"))


def _fred_obs(api_key: str, series_id: str, start: str, **extra) -> dict:
    """{date: value} for one FRED series from `start` (missing values dropped) — the fallback."""
    url = "https://api.stlouisfed.org/fred/series/observations"
    params = {"series_id": series_id, "api_key": api_key, "file_type": "json",
              "observation_start": start, **extra}
    r = requests.get(url, params=params, headers=HEADERS, timeout=30)
    r.raise_for_status()
    out = {}
    for o in r.json().get("observations", []):
        v = o.get("value")
        if v not in (None, ".", ""):
            out[o["date"]] = float(v)
    return out


def _on_or_before(series: dict, iso: str):
    ks = [k for k in series if k <= iso]
    return series[max(ks)] if ks else None


RANGE_FROM = "2008-12-16"   # the FOMC's first target RANGE (0-0.25%); a single target before


def year_end_targets(upper: dict, lower: dict, single: dict, today) -> dict:
    """year_end[Y] = the policy target in force on December 31 of Y, the concept the dots
    project: the MIDPOINT of the target range from December 2008, and the single target rate
    before that (the Board's openmarket.htm; FRED's DFEDTARU / DFEDTARL / DFEDTAR only when
    the Board's page fails). Only completed years appear.

    Until 2026-10-23 this was proxied by the following January's average effective funds
    rate, which turned a projection that landed exactly on the range the Fed set (December
    2016's 1.375 for end-2017; the range was 1.25-1.50) into a +0.04 "miss" (Jason's call
    after the outside review: score the dots against the midpoint). Pure, for the test."""
    years = sorted({int(k[:4]) for k in list(upper) + list(lower) + list(single)})
    out = {}
    for y in years:
        if dt.date(y, 12, 31) > today:
            continue
        iso = f"{y}-12-31"
        if iso >= RANGE_FROM:
            u, lo = _on_or_before(upper, iso), _on_or_before(lower, iso)
            if u is None or lo is None:
                continue
            out[str(y)] = round((u + lo) / 2, 3)
        else:
            t = _on_or_before(single, iso)
            if t is not None:
                out[str(y)] = round(t, 3)
    return out


def current_rate(daily: dict, monthly: dict):
    """The "Current" reference line: the latest DAILY effective rate, else the latest monthly
    average (the monthly series was up to five weeks stale and could sit at the pre-decision
    rate on the very day a meeting moved it — hence daily first)."""
    if daily:
        d0 = max(daily)
        return {"ym": d0, "rate": round(daily[d0], 3)}
    if monthly:
        m0 = max(monthly)
        return {"ym": m0[:7], "rate": round(monthly[m0], 3)}
    return None


def fetch_actual(api_key: str):
    """Return (year_end, current, credits): the Board first for each leg, FRED (when a key is
    set) only for a leg whose Board source failed. `credits` names what served each leg."""
    credits = {}
    try:
        upper, lower, single = board_targets()
        credits["target"] = "board"
    except Exception as e:  # noqa: BLE001
        if not api_key:
            raise
        log(f"! the Board's target page failed ({_scrub(str(e))}); falling back to FRED")
        upper = _fred_obs(api_key, "DFEDTARU", RANGE_FROM)
        lower = _fred_obs(api_key, "DFEDTARL", RANGE_FROM)
        single = _fred_obs(api_key, "DFEDTAR", "2008-01-01")     # ends 2008-12-15
        credits["target"] = "fred"
    year_end = year_end_targets(upper, lower, single, dt.date.today())
    current = None
    try:
        daily, monthly = board_h15_fedfunds()
        current = current_rate(daily, monthly)
        credits["current"] = "board"
    except Exception as e:  # noqa: BLE001
        if api_key:
            log(f"! the Board's H.15 package failed ({_scrub(str(e))}); falling back to FRED")
            monthly = {k: v for k, v in _fred_obs(api_key, "FEDFUNDS", "2024-01-01").items()}
            try:  # prefer the daily series; the monthly one is the fallback
                daily = _fred_obs(api_key, "DFF", "2024-01-01", sort_order="desc", limit=5)
            except Exception as e2:  # noqa: BLE001
                log(f"! DFF fetch failed ({_scrub(str(e2))}); using monthly FEDFUNDS for 'current'")
                daily = {}
            current = current_rate(daily, monthly)
            credits["current"] = "fred"
        else:
            log(f"! the Board's H.15 package failed ({_scrub(str(e))}) and FRED_API_KEY is not set; "
                f"no 'current' line")
    return year_end, current, credits


def source_line(credits: dict) -> str:
    """The data.json `source` credit, naming only what actually served this run."""
    tgt = ("the Federal Reserve Board's target history (openmarket.htm)" if credits.get("target") == "board"
           else "FRED DFEDTARU/DFEDTARL (DFEDTAR before December 2008), the Board's page having failed")
    cur = {"board": "the Board's H.15 effective federal funds rate (daily)",
           "fred": "FRED DFF (the Board's H.15 having failed)"}.get(credits.get("current"))
    out = ("Federal Reserve, Summary of Economic Projections, Figure 2 (accessible HTML). "
           f"Realized rate: the year-end target-range midpoint (the single target before "
           f"December 2008), {tgt}")
    return out + (f"; current: {cur}." if cur else ".")


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
    ap.add_argument("--no-actual", "--no-fred", dest="no_actual", action="store_true",
                    help="skip the realized-rate overlay (offline); --no-fred is the old name")
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
            r = get(PROJ_URL_EPA.format(date=d))  # pre-2013: the minutes' own appendix
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

    year_end, current, credits = {}, None, {}
    api_key = os.environ.get("FRED_API_KEY", "").strip()
    if not args.no_actual:
        try:
            year_end, current, credits = fetch_actual(api_key)
            log(f"target midpoints ({credits.get('target')}): realized year-ends {min(year_end)}–"
                f"{max(year_end)}; current ({credits.get('current')}) "
                f"{current['rate'] if current else '—'}% ({current['ym'] if current else '—'})")
        except Exception as e:  # noqa: BLE001
            log(f"! realized-rate fetch failed ({_scrub(str(e))}); continuing without overlay")

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
        "source": source_line(credits),
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
