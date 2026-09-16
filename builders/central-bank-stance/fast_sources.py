#!/usr/bin/env python3
"""
Same-day policy-rate feeds for the majors — the fast lane over BIS.

BIS WS_CBPOL is the spine of the Stance Matrix: one keyless source, all 12 banks,
full history. Its one weakness is latency — it publishes in batches, so the series
routinely ends a week behind (on 2026-09-16 it stopped at 2026-09-08, with the ECB
still shown at 2.25% the day its 2.50% took effect).

This module adds a second, faster reading for the four banks that publish their own
policy rate on a machine-readable feed. Each function returns a chronological
[(iso_date, value)] on EXACTLY the same basis as the BIS series for that bank, so
build.py can splice the newer observations onto the end of the BIS history and let
every derived field (last move, phase, YTD, sparkline) recompute unchanged.

  US  Federal Reserve      FRED DFEDTARU/DFEDTARL midpoint   same day   needs FRED_API_KEY
  XM  ECB                  ECB Data Portal, deposit facility same day   keyless
  GB  Bank of England      Bank Rate (IADB IUDBEDR)          ~1 day     keyless
  CA  Bank of Canada       Valet V39079, target overnight    ~1 day     keyless

The basis is verified at run time, not assumed: build.py compares each feed against
BIS on BIS's own last date and refuses to splice if they disagree. A feed that breaks,
times out or drifts is simply dropped for that run and the row falls back to BIS.

These are EFFECTIVE-DATE series — they move when a new rate takes effect, which is
the day after an FOMC decision and the Wednesday after a Governing Council one. They
are not a decision wire; nothing here reads a policy statement.
"""

from __future__ import annotations

import csv
import io
import json
import os
import urllib.request
from datetime import date, datetime, timedelta

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
def fed_us(start: str) -> list[tuple[str, float]]:
    """Target-range midpoint, to match BIS's single value for the US."""
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


FAST = {
    "US": ("FRED (Board of Governors) — federal funds target range", fed_us),
    "XM": ("ECB Data Portal — deposit facility rate", ecb_xm),
    "GB": ("Bank of England — Bank Rate (IUDBEDR)", boe_gb),
    "CA": ("Bank of Canada — target for the overnight rate", boc_ca),
}


def lookback_start(days: int = 120) -> str:
    return (date.today() - timedelta(days=days)).isoformat()
