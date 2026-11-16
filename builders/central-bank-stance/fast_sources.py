#!/usr/bin/env python3
"""
Same-day policy-rate feeds for the majors — the fast lane over BIS.

BIS WS_CBPOL is the spine of the Stance Matrix: one keyless source, all 12 banks,
full history. Its one weakness is latency — it publishes in batches, so the series
routinely ends a week behind (on 2026-09-16 it stopped at 2026-09-08, with the ECB
still shown at 2.25% the day its 2.50% took effect).

This module adds a second, faster reading for the five banks that publish their own
policy rate where a script can read it. Each function returns a chronological
[(iso_date, value)] on EXACTLY the same basis as the BIS series for that bank, so
build.py can splice the newer observations onto the end of the BIS history and let
every derived field (last move, phase, YTD, sparkline) recompute unchanged.

  US  Federal Reserve      the Board's openmarket.htm,       same day   keyless; HTML, not a feed;
                           target-range midpoint                        FRED DFEDTARU/L (FRED_API_KEY)
                                                                        only if the Board fails
  XM  ECB                  ECB Data Portal, deposit facility same day   keyless
  GB  Bank of England      Bank Rate (IADB IUDBEDR)          ~1 day     keyless
  CA  Bank of Canada       Valet V39079, target overnight    ~1 day     keyless
  IN  Reserve Bank of India  rbi.org.in home page + MPC      same day   keyless; HTML, not a feed
                             resolution press releases

The Fed's is read off a web page too: the Board publishes its target range as a table of
changes on openmarket.htm (the page macrolib's `frb:FOMC/…` reader parses), not as a feed. It
moved there from FRED on 2026-11-15 (the FRED round: everything econvitals.org shows reads its
original publisher where it can); FRED stays only as the fallback for a run where the Board's
page fails, and the row then says FRED served it.

The RBI one is different in kind: the RBI publishes no machine-readable series (its data
files sit behind a bot wall, DBIE is an encrypted gateway), so `rbi_in` (macrolib's
`rbi.py` since 2026-11-15, the one RBI parser — see the India section) reads the bank's
own web pages — the home page's "Policy Repo Rate : X%" box for today's value and the
Monetary Policy Statements listing for the decision dates and sizes. Added 2026-10-21 at
Jason's word, reversing his 2026-10-09 ruling not to scrape the RBI; the BIS relay of
India stopped at 2026-07-23 and missed the October hike. Every parse is guarded (a missing
sentence or a rate outside 0-20 raises, so build.py falls back to BIS) and the home page
must agree with the latest resolution, or nothing is spliced.

The basis is verified at run time, not assumed: build.py compares each feed against
BIS on BIS's own last date and refuses to splice if they disagree. A feed that breaks,
times out or drifts is simply dropped for that run and the row falls back to BIS.

These are EFFECTIVE-DATE series — they move when a new rate takes effect, which is
the day after an FOMC decision and the Wednesday after a Governing Council one. They
are not a decision wire; nothing here reads a policy statement.
"""

from __future__ import annotations

import csv
import html
import io
import json
import os
import re
import sys
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta, timezone

UA = {"User-Agent": "jfmacro-lab/1.0"}
TIMEOUT = 45


def _get(url: str) -> str:
    return urllib.request.urlopen(
        urllib.request.Request(url, headers=UA), timeout=TIMEOUT
    ).read().decode("utf-8", "replace")


def _clean(pairs) -> list[tuple[str, float]]:
    """Drop blanks/placeholders, coerce to float, sort chronologically."""
    out: list[tuple[str, float]] = []
    for d, v in pairs:
        if not d or v in (None, "", ".", "NaN", "ND"):
            continue
        try:
            out.append((d, float(v)))
        except (TypeError, ValueError):
            continue
    out.sort(key=lambda t: t[0])
    return out


# ----------------------------------------------------------------- United States
# The Board's own page, read with plain HTTP (the Action has no macrolib): one table per year of
# CHANGES ONLY — Date | Increase | Decrease (basis points) | Level (%) — 2003 on, each row dated
# by the day the new target or range TOOK EFFECT. The parse mirrors macrolib/frb.py's
# `frb:FOMC/…` reader, guard for guard:
#   * every other row is `<tr class="alt">` — a `<tr>` pattern would drop half the history while
#     every row it kept still parsed, so rows are `<tr` with any attributes;
#   * the Board's own identity is checked on every row (its change column = the difference
#     between its level and the row before it), so a missing or misread row is REFUSED, never
#     carried silently into the step series;
#   * a 200 without the table heading (a courtesy page, a redesign) is refused, not read as
#     "no changes".
# Known difference from FRED's DFEDTARU/L, checked 2026-11-15 over 2008-12-16..2026-11-15:
# identical on 6,542 of 6,544 calendar days; FRED dates the first two hikes of the range era
# by the announcement (2015-12-16, 2016-12-14), the Board by the effective day, one day later.
FOMC_TARGET_URL = "https://www.federalreserve.gov/monetarypolicy/openmarket.htm"
FED_BOARD_LABEL = "Federal Reserve Board — federal funds target range (openmarket.htm)"
FED_FRED_LABEL = ("Federal Reserve Board via FRED (DFEDTARU/DFEDTARL) — federal funds target "
                  "range; the Board's own page failed this run")
_FOMC_HEADING = "target federal funds rate or range"
_FOMC_YEAR = re.compile(r"<h4>\s*(\d{4})\s*</h4>(.*?)</table>", re.S)
_FOMC_ROW = re.compile(r"<tr\b[^>]*>(.*?)</tr>", re.S)
_FOMC_CELL = re.compile(r"<t([dh])\b[^>]*>(.*?)</t[dh]>", re.S)
_FOMC_LEVEL = re.compile(r"^(\d+(?:\.\d+)?)(?:-(\d+(?:\.\d+)?))?$")


def _cell_text(s: str) -> str:
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", "", s))).replace("\xa0", " ").strip()


def _fomc_bp(cell: str):
    """'25' -> (25, 25); '75-100' -> (75, 100) (December 2008: the upper bound fell 75bp, the
    lower 100bp); '...' / '0' -> None."""
    c = cell.strip()
    if c in ("", "...", "…", "0", "—", "-"):
        return None
    m = re.match(r"^(\d+)(?:\s*-\s*(\d+))?$", c)
    if not m:
        raise ValueError(f"FOMC target page: change cell {cell!r} is not basis points")
    a = int(m.group(1))
    return (a, int(m.group(2)) if m.group(2) else a)


def parse_fomc_target(page: str) -> list[tuple[str, float, float]]:
    """[(effective ISO date, lower, upper)] ascending, off the Board's openmarket.htm. A single
    target (before 2008-12-16) has lower == upper. Raises ValueError on anything that does not
    read as the Board's table."""
    if len(page) < 2048 or _FOMC_HEADING not in page:
        raise ValueError(f"FOMC target page answered ({len(page):,} chars) without its table "
                         f"heading {_FOMC_HEADING!r}; refused")
    rows = []
    for ym in _FOMC_YEAR.finditer(page):
        year = int(ym.group(1))
        for rm in _FOMC_ROW.finditer(ym.group(2)):
            cells = _FOMC_CELL.findall(rm.group(1))
            if not cells or cells[0][0] == "h":
                continue                                   # the header row
            vals = [_cell_text(c) for _, c in cells]
            if len(vals) != 4:
                raise ValueError(f"FOMC target page: a {year} row has {len(vals)} cells ({vals})")
            day = vals[0].rstrip("*† ").strip()           # "March 4*" — the Board's 2024 correction
            d = None
            for fmt in ("%B %d %Y", "%b %d %Y", "%b. %d %Y"):
                try:
                    d = datetime.strptime(f"{day} {year}", fmt).date()
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
    for prev, cur in zip(rows, rows[1:]):                 # the Board's own identity
        up, down = cur[3], cur[4]
        if (up is None) == (down is None):
            raise ValueError(f"FOMC target page: the {cur[0]} row is neither an increase nor a decrease")
        sign, (a, b) = (1, up) if up is not None else (-1, down)
        if {round((cur[1] - prev[1]) * 100), round((cur[2] - prev[2]) * 100)} != {sign * a, sign * b}:
            raise ValueError(f"FOMC target page: the level chain breaks at {cur[0]} (row before: "
                             f"{prev[0]} {prev[1]:g}-{prev[2]:g}); a change is missing or misread")
    return [(r[0].isoformat(), r[1], r[2]) for r in rows]


def _eastern_today() -> date:
    """Today in Washington: the FOMC dates its decisions there, and after 8pm Eastern the UTC
    date (the Actions runner's) is already tomorrow."""
    try:
        from zoneinfo import ZoneInfo
        return datetime.now(ZoneInfo("America/New_York")).date()
    except Exception:  # noqa: BLE001 — no tz database: EST is never more than an hour off
        return (datetime.now(timezone.utc) - timedelta(hours=5)).date()


def target_midpoints(rows: list[tuple[str, float, float]], start: str,
                     through: str) -> list[tuple[str, float]]:
    """One observation per CALENDAR day from `start` through `through` (FRED's layout): the
    midpoint of the target range in force that day (the single target before 2008-12-16).
    Raises when the page starts after `start` (no level known for the opening days)."""
    if not rows or rows[0][0] > start:
        raise ValueError(f"FOMC target page starts {rows[0][0] if rows else '—'}, after {start}")
    out, i = [], 0
    d, last = date.fromisoformat(start), date.fromisoformat(through)
    while d <= last:
        iso = d.isoformat()
        while i + 1 < len(rows) and rows[i + 1][0] <= iso:
            i += 1
        out.append((iso, (rows[i][1] + rows[i][2]) / 2))
        d += timedelta(days=1)
    return out


def fed_us_board(start: str, fetch=None) -> list[tuple[str, float]]:
    """Target-range midpoint from the Board's own page, `start` through today (Eastern) — or
    through the newest row when that is later: on a decision afternoon the Board posts the new
    range dated TOMORROW, its effective day, and the series carries it (as FRED's does)."""
    rows = parse_fomc_target((fetch or _get)(FOMC_TARGET_URL))
    through = max(_eastern_today().isoformat(), rows[-1][0])
    return target_midpoints(rows, start, through)


def fed_us_fred(start: str) -> list[tuple[str, float]]:
    """The failure-only fallback: FRED's DFEDTARU/DFEDTARL midpoint."""
    key = os.environ.get("FRED_API_KEY", "").strip()
    if not key:
        raise RuntimeError("FRED_API_KEY not set")
    legs = {}
    for sid in ("DFEDTARU", "DFEDTARL"):
        url = ("https://api.stlouisfed.org/fred/series/observations"
               f"?series_id={sid}&api_key={key}&file_type=json"
               f"&observation_start={start}")
        obs = json.loads(_get(url)).get("observations", [])
        legs[sid] = dict(_clean((o.get("date"), o.get("value")) for o in obs))
    both = set(legs["DFEDTARU"]) & set(legs["DFEDTARL"])
    return _clean(
        (d, (legs["DFEDTARU"][d] + legs["DFEDTARL"][d]) / 2) for d in both
    )


def fed_us(start: str) -> list[tuple[str, float]]:
    """Target-range midpoint, to match BIS's single value for the US: the Board's own page,
    FRED only when the Board fails. `fed_us.served_by` names the one that answered (build.py's
    `apply_fast` puts it on the row)."""
    fed_us.served_by = None
    try:
        out = fed_us_board(start)
        fed_us.served_by = FED_BOARD_LABEL
        return out
    except Exception as e:  # noqa: BLE001
        if not os.environ.get("FRED_API_KEY", "").strip():
            raise RuntimeError(f"the Board's target page failed ({e!r}) and FRED_API_KEY is not "
                               f"set for the fallback") from e
        print(f"[US] the Board's target page failed ({e!r}); falling back to FRED", file=sys.stderr)
        out = fed_us_fred(start)
        fed_us.served_by = FED_FRED_LABEL
        return out


fed_us.served_by = None


# -------------------------------------------------------------------- Euro area
def ecb_xm(start: str) -> list[tuple[str, float]]:
    """Deposit facility rate — what BIS reports for XM."""
    url = ("https://data-api.ecb.europa.eu/service/data/FM/D.U2.EUR.4F.KR.DFR.LEV"
           f"?format=csvdata&startPeriod={start}")
    rows = csv.DictReader(io.StringIO(_get(url)))
    return _clean((r.get("TIME_PERIOD"), r.get("OBS_VALUE")) for r in rows)


# --------------------------------------------------------------- United Kingdom
def boe_gb(start: str) -> list[tuple[str, float]]:
    """Bank Rate. The IADB speaks dd/Mon/yyyy in and 'dd Mon yyyy' out."""
    frm = datetime.strptime(start, "%Y-%m-%d").strftime("%d/%b/%Y")
    to = date.today().strftime("%d/%b/%Y")
    url = ("https://www.bankofengland.co.uk/boeapps/database/_iadb-fromshowcolumns.asp"
           f"?csv.x=yes&Datefrom={frm}&Dateto={to}&SeriesCodes=IUDBEDR"
           "&CSVF=TN&UsingCodes=Y&VPD=Y&VFD=N")
    rows = csv.DictReader(io.StringIO(_get(url)))
    pairs = []
    for r in rows:
        raw = (r.get("DATE") or "").strip()
        if not raw:
            continue
        try:
            iso = datetime.strptime(raw, "%d %b %Y").date().isoformat()
        except ValueError:
            continue
        pairs.append((iso, r.get("IUDBEDR")))
    return _clean(pairs)


# ----------------------------------------------------------------------- Canada
def boc_ca(start: str) -> list[tuple[str, float]]:
    """Target for the overnight rate (Valet series V39079)."""
    url = ("https://www.bankofcanada.ca/valet/observations/V39079/json"
           f"?start_date={start}")
    obs = json.loads(_get(url)).get("observations", [])
    return _clean((o.get("d"), (o.get("V39079") or {}).get("v")) for o in obs)


# ------------------------------------------------------------------------ India
# The RBI reader is macrolib's since 2026-11-15 (`macrolib/rbi.py`): ONE parser for this matrix
# and for `india:POLICY_RATE`, which puts India back on the chartbook's policy-rate charts — the
# 2026-10-21 ruling said to port it rather than write a second one. This Action has no macrolib
# (a private repo; the workflow installs only pyyaml and openpyxl), so `rbi.py` beside this file
# is a BYTE-IDENTICAL copy of macrolib's, imported only where macrolib is not installed.
# test_rbi_copy.py here and macrolib's tests/test_rbi.py fail the moment the two differ. Edit
# macrolib/rbi.py and copy it over — never edit the copy. Its parses are unchanged from the
# 2026-10-21 reader that lived here (test_build.py's RBI tests run against it untouched).
try:
    from macrolib import rbi as _rbi
except ImportError:                   # the GitHub Action: the copy beside this file
    import rbi as _rbi

RBI_HOME = _rbi.RBI_HOME
RBI_LISTING = _rbi.RBI_LISTING
RBI_RATE_RANGE = _rbi.RBI_RATE_RANGE
RBI_MAX_YEARS_BACK = _rbi.RBI_MAX_YEARS_BACK
parse_rbi_home = _rbi.parse_rbi_home
parse_rbi_listing = _rbi.parse_rbi_listing
parse_rbi_listing_form = _rbi.parse_rbi_listing_form
parse_rbi_resolution = _rbi.parse_rbi_resolution
rbi_in = _rbi.rbi_in                  # (start, fetch=None, post=None, today=None) -> [(iso, rate)]


FAST = {
    "US": (FED_BOARD_LABEL, fed_us),
    "XM": ("ECB Data Portal — deposit facility rate", ecb_xm),
    "GB": ("Bank of England — Bank Rate (IUDBEDR)", boe_gb),
    "CA": ("Bank of Canada — target for the overnight rate", boc_ca),
    "IN": ("Reserve Bank of India — Policy Repo Rate (rbi.org.in home page + MPC resolutions)", rbi_in),
}


def lookback_start(days: int = 120) -> str:
    return (date.today() - timedelta(days=days)).isoformat()
