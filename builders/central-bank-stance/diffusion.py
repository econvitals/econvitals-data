#!/usr/bin/env python3
"""
Global monetary policy diffusion index — every central bank in the BIS policy-rate dataset.

Companion to build.py (the 12-bank stance matrix). Where the matrix looks closely at a dozen
banks, this reads ALL the daily policy-rate series BIS publishes (WS_CBPOL, ~41 economies with
data since 2000) and reduces them to one monthly line from 2000-01 to the current month:

    index = (banks hiking − banks cutting) / banks with data, in percent

A bank's stance at a month-end comes from its own rate history alone: its most recent rate
change on or before that month-end makes it `hiking` (an increase) or `cutting` (a decrease)
if the change fell in that month or the two before it (WINDOW_MONTHS = 3: a hike in month t
counts for t, t+1, t+2 and the bank is on `hold` from t+3), else `hold`. A bank counts in a
month's denominator when BIS has an observation for it in that same three-month window; a
series that has gone dark for longer (Argentina stopped at 2025-07) leaves the denominator
rather than being carried as a stale hold, and comes back if BIS resumes it.

The fetch is ONE request: the SDMX wildcard key `D.` with `detail=dataonly` returns every
series in a 7MB four-column CSV (FREQ, REF_AREA, TIME_PERIOD, OBS_VALUE) in about five
seconds; without `detail=dataonly` the same call is 145MB because the attribute text repeats
on every row. If the wildcard fails, the area list is read from the dataflow's availability
endpoint (falling back to the codes seen on 2026-10-25) and each area is fetched on its own
in a small thread pool; a bank whose fetch fails is dropped from that run and named in
`meta.dropped`. The data-only CSV carries no bank names, so NAMES maps code -> bank; when a
CSV does carry SOURCE_REF (the attribute layout) that column wins.

Euro area: BIS carries the ECB as XM and stops each member's national series at its euro
adoption (Greece ends 2000-12, Croatia 2022-12; the 1999 founders never reach 2000), so the
members only ever count as independent banks before joining and the ECB is the one euro-area
entry after. EURO_ADOPTION enforces that cut even if BIS one day extends a national series.

No network call raises out of this module: a dead API yields an empty index with the error in
`meta.error`, so the caller publishes nothing stale and sees why.

    from diffusion import build_diffusion
    build_diffusion()            # live
    build_diffusion(fetch=fn)    # offline: fn(url) -> CSV/JSON text (see test_diffusion.py)
"""

from __future__ import annotations

import csv
import io
import json
import sys
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timezone

SOURCE = "BIS — Central bank policy rates (WS_CBPOL)"
SOURCE_URL = "https://data.bis.org/topics/CBPOL"
API = "https://stats.bis.org/api/v2"
FLOW = "dataflow/BIS/WS_CBPOL/1.0"
# Fetch from late 1999 so a change in the last weeks of 1999 is known when 2000-01 is scored;
# the published arrays start at FIRST_MONTH.
FETCH_START = "1999-10-01"
FIRST_MONTH = "2000-01"
WINDOW_MONTHS = 3
WILDCARD_URL = f"{API}/data/{FLOW}/D.?startPeriod={FETCH_START}&format=csv&detail=dataonly"
AREA_URL = f"{API}/data/{FLOW}/D.{{area}}?startPeriod={FETCH_START}&format=csv&detail=dataonly"
AVAILABILITY_URL = f"{API}/availability/{FLOW}/D.*"
USER_AGENT = "jfmacro-lab/1.0"
TIMEOUT = 60
MAX_WORKERS = 6

# Bank names for the data-only CSV, taken from the dataset's own SOURCE_REF attribute on
# 2026-10-25 (the 1999 euro founders carry no data after 2000 and are listed for completeness).
NAMES = {
    "AR": "Central Bank of Argentina", "AT": "Oesterreichische Nationalbank",
    "AU": "Reserve Bank of Australia", "BE": "National Bank of Belgium",
    "BR": "Central Bank of Brazil", "CA": "Bank of Canada", "CH": "Swiss National Bank",
    "CL": "Central Bank of Chile", "CN": "People's Bank of China",
    "CO": "Central Bank of Colombia", "CZ": "Czech National Bank",
    "DE": "Deutsche Bundesbank", "DK": "Danmarks Nationalbank", "ES": "Banco de España",
    "FR": "Banque de France", "GB": "Bank of England", "GR": "Bank of Greece",
    "HK": "Hong Kong Monetary Authority", "HR": "Croatian National Bank",
    "HU": "Hungarian National Bank", "ID": "Bank Indonesia", "IL": "Bank of Israel",
    "IN": "Reserve Bank of India", "IS": "Central Bank of Iceland", "IT": "Banca d'Italia",
    "JP": "Bank of Japan", "KR": "Bank of Korea", "KW": "Central Bank of Kuwait",
    "MA": "Bank Al-Maghrib (Central Bank of Morocco)",
    "MK": "National Bank of the Republic of Macedonia", "MX": "Bank of Mexico",
    "MY": "Bank Negara Malaysia", "NL": "De Nederlandsche Bank", "NO": "Norges Bank",
    "NZ": "Reserve Bank of New Zealand", "PE": "Central Reserve Bank of Peru",
    "PH": "Bangko Sentral ng Pilipinas", "PL": "National Bank of Poland",
    "PT": "Banco de Portugal", "RO": "National Bank of Romania",
    "RS": "National Bank of Serbia", "RU": "Bank of Russia",
    "SA": "Saudi Arabian Monetary Authority", "SE": "Sveriges Riksbank",
    "TH": "Bank of Thailand", "TR": "Central Bank of the Republic of Turkey",
    "US": "US Federal Reserve System", "XM": "European Central Bank",
    "ZA": "South African Reserve Bank",
}
# The REF_AREA list the availability endpoint returned on 2026-10-25 — the last-resort area
# list when both the wildcard and the availability call fail.
KNOWN_AREAS = sorted(NAMES)

# First month in which a country's policy rate is the ECB's. A national observation dated in
# or after that month is dropped so the euro area is never counted twice (BIS already stops
# the national series there; this makes the rule explicit and robust).
EURO_ADOPTION = {
    "AT": "1999-01", "BE": "1999-01", "DE": "1999-01", "ES": "1999-01", "FI": "1999-01",
    "FR": "1999-01", "IE": "1999-01", "IT": "1999-01", "LU": "1999-01", "NL": "1999-01",
    "PT": "1999-01", "GR": "2001-01", "SI": "2007-01", "CY": "2008-01", "MT": "2008-01",
    "SK": "2009-01", "EE": "2011-01", "LV": "2014-01", "LT": "2015-01", "HR": "2023-01",
    "BG": "2026-01",
}


# ----------------------------------------------------------------------------- fetch
def fetch_text(url: str) -> str:
    """GET `url` as text (BIS CSV or JSON). Raises on any failure; callers catch."""
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    return urllib.request.urlopen(req, timeout=TIMEOUT).read().decode("utf-8")


def parse_csv(text: str):
    """BIS SDMX CSV -> ({code: [(iso_date, value)] chronological}, {code: name}).

    Works on both layouts: the four-column `detail=dataonly` one and the full attribute one
    (whose SOURCE_REF column supplies the bank name). Blank/NaN observations (non-trading
    days in some series) are skipped. A national euro-area observation dated at or after the
    country's adoption month is dropped (see EURO_ADOPTION).
    """
    series: dict[str, list[tuple[str, float]]] = {}
    names: dict[str, str] = {}
    for row in csv.DictReader(io.StringIO(text)):
        code, d, v = row.get("REF_AREA"), row.get("TIME_PERIOD"), row.get("OBS_VALUE")
        if not code or not d or v in (None, "", "NaN"):
            continue
        try:
            val = float(v)
        except ValueError:
            continue
        adopted = EURO_ADOPTION.get(code)
        if adopted and d[:7] >= adopted:
            continue
        series.setdefault(code, []).append((d, val))
        src = (row.get("SOURCE_REF") or "").strip()
        if src and code not in names:
            names[code] = src
    for pts in series.values():
        pts.sort(key=lambda t: t[0])
    return series, names


def discover_areas(fetch) -> list[str]:
    """REF_AREA codes from the dataflow's availability constraint; KNOWN_AREAS if that fails."""
    try:
        doc = json.loads(fetch(AVAILABILITY_URL))
        for cons in doc["data"]["dataConstraints"]:
            for region in cons.get("cubeRegions", []):
                for kv in region.get("keyValues", []):
                    if kv.get("id") == "REF_AREA":
                        codes = sorted({v["value"] for v in kv["values"]})
                        if codes:
                            return codes
    except Exception as e:  # noqa: BLE001
        print(f"[diffusion] availability call failed ({e!r}); using the known area list",
              file=sys.stderr)
    return list(KNOWN_AREAS)


def load_series(fetch):
    """All policy-rate series: the wildcard call first, per-area calls in a thread pool if it
    fails. Returns (series, names, info) with info = {mode, dropped, error}; never raises."""
    info = {"mode": "wildcard", "dropped": [], "error": None}
    try:
        series, names = parse_csv(fetch(WILDCARD_URL))
        if series:
            return series, names, info
        raise ValueError("wildcard call returned no observations")
    except Exception as e:  # noqa: BLE001
        print(f"[diffusion] wildcard fetch failed ({e!r}); fetching per area", file=sys.stderr)
        info["mode"] = "per-area"

    areas = discover_areas(fetch)

    def one(area: str):
        try:
            return area, parse_csv(fetch(AREA_URL.format(area=area))), None
        except Exception as e:  # noqa: BLE001
            return area, None, repr(e)

    series: dict[str, list[tuple[str, float]]] = {}
    names: dict[str, str] = {}
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        for area, parsed, err in pool.map(one, areas):
            if err:
                print(f"[diffusion] {area} fetch failed ({err}); dropped this run", file=sys.stderr)
                info["dropped"].append(area)
                continue
            s, n = parsed
            series.update(s)
            names.update(n)
    if not series:
        info["error"] = "no BIS policy-rate series could be fetched (wildcard and per-area calls both failed)"
    return series, names, info


# ----------------------------------------------------------------------------- derive
def month_range(first: str, last: str) -> list[str]:
    """Inclusive list of "YYYY-MM" strings."""
    y, m = int(first[:4]), int(first[5:7])
    ly, lm = int(last[:4]), int(last[5:7])
    out = []
    while (y, m) <= (ly, lm):
        out.append(f"{y:04d}-{m:02d}")
        m += 1
        if m == 13:
            y, m = y + 1, 1
    return out


def months_apart(a: str, b: str) -> int:
    """b − a in whole months for "YYYY-MM" strings (b >= a gives a non-negative answer)."""
    return (int(b[:4]) - int(a[:4])) * 12 + (int(b[5:7]) - int(a[5:7]))


def changes(series: list[tuple[str, float]]) -> list[tuple[str, int]]:
    """Every rate change as (iso_date, +1 for up / -1 for down), chronological."""
    out = []
    last = None
    for d, v in series:
        if last is not None and v != last:
            out.append((d, 1 if v > last else -1))
        last = v
    return out


def stance_by_month(series: list[tuple[str, float]], months: list[str],
                    window: int = WINDOW_MONTHS) -> list[str | None]:
    """One bank's stance for each month in `months`: "hiking" / "cutting" / "hold", or None
    where the bank has no observation in that month or the (window − 1) before it."""
    obs_months = {d[:7] for d, _ in series}
    chg = changes(series)
    out: list[str | None] = []
    i = 0
    last_chg = None  # (month, direction) of the latest change on or before this month-end
    for m in months:
        while i < len(chg) and chg[i][0][:7] <= m:
            last_chg = (chg[i][0][:7], chg[i][1])
            i += 1
        if not (obs_months & window_set(m, window)):
            out.append(None)
            continue
        if last_chg and months_apart(last_chg[0], m) < window:
            out.append("hiking" if last_chg[1] > 0 else "cutting")
        else:
            out.append("hold")
    return out


def window_set(m: str, window: int) -> set[str]:
    """The `window` months ending at m, as "YYYY-MM" strings."""
    y, mo = int(m[:4]), int(m[5:7])
    out = set()
    for _ in range(window):
        out.add(f"{y:04d}-{mo:02d}")
        mo -= 1
        if mo == 0:
            y, mo = y - 1, 12
    return out


def bank_name(code: str, names: dict[str, str]) -> str:
    return names.get(code) or NAMES.get(code) or code


# ----------------------------------------------------------------------------- build
def build_diffusion(fetch=None, today: date | None = None) -> dict:
    """The diffusion index payload (see the module docstring for the rules).

    `fetch(url) -> str` replaces the network for tests. `today` sets the last month scored
    (the current month, scored on the data available so far)."""
    fetch = fetch or fetch_text
    today = today or datetime.now(timezone.utc).date()
    last_month = today.strftime("%Y-%m")
    months = month_range(FIRST_MONTH, last_month)
    generated = datetime.now(timezone.utc).isoformat(timespec="seconds")

    series, names, info = load_series(fetch)
    meta = {
        "source": SOURCE,
        "source_url": SOURCE_URL,
        "n_banks": 0,
        "n_series": len(series),
        "window_months": WINDOW_MONTHS,
        "generated_at": generated,
        "fetch_mode": info["mode"],
        "dropped": info["dropped"],
        "not_counted_latest": [],
        "error": info["error"],
        "note": "",
    }
    if not series:
        return {"meta": meta, "months": [], "index": [], "hiking": [], "cutting": [], "hold": [],
                "n": [], "latest": {"month": last_month, "hiking": [], "cutting": [], "hold": []}}

    stances = {code: stance_by_month(pts, months) for code, pts in series.items()}
    hiking, cutting, hold, n, index = [], [], [], [], []
    for k in range(len(months)):
        h = sum(1 for s in stances.values() if s[k] == "hiking")
        c = sum(1 for s in stances.values() if s[k] == "cutting")
        o = sum(1 for s in stances.values() if s[k] == "hold")
        tot = h + c + o
        hiking.append(h)
        cutting.append(c)
        hold.append(o)
        n.append(tot)
        index.append(round(100.0 * (h - c) / tot, 1) if tot else None)

    latest = {"month": last_month, "hiking": [], "cutting": [], "hold": []}
    not_counted = []
    for code in sorted(series, key=lambda c: bank_name(c, names)):
        s = stances[code][-1]
        entry = {"code": code, "name": bank_name(code, names)}
        if s is None:
            euro = EURO_ADOPTION.get(code)
            not_counted.append(entry | {"last": series[code][-1][0],
                                        "why": f"euro area since {euro}" if euro else
                                        f"no BIS observation in the last {WINDOW_MONTHS} months"})
        else:
            latest[s].append(entry)
    meta["n_banks"] = n[-1]
    meta["not_counted_latest"] = not_counted
    dropped_note = (f" {len(info['dropped'])} bank(s) could not be fetched this run and are left "
                    f"out of this run's denominator: {', '.join(info['dropped'])}."
                    if info["dropped"] else "")
    meta["note"] = (
        f"Each bank's stance at a month-end is read from its own BIS policy-rate history: hiking "
        f"(cutting) if its most recent rate change was an increase (decrease) in that month or the "
        f"{WINDOW_MONTHS - 1} before it, else hold. The index is (hiking − cutting) ÷ banks with "
        f"data, in percent; a bank counts in a month only if BIS has an observation for it within "
        f"the same {WINDOW_MONTHS}-month window, so a series BIS has stopped relaying leaves the "
        f"denominator. The euro area enters once, as the ECB (XM): BIS ends each member's national "
        f"series at euro adoption and this builder enforces that cut. The current month is scored "
        f"on the data published so far. "
        + ("Fetched in one SDMX wildcard call." if info["mode"] == "wildcard" else
           "The wildcard call failed this run; each bank was fetched on its own.")
        + dropped_note
    )
    return {"meta": meta, "months": months, "index": index, "hiking": hiking, "cutting": cutting,
            "hold": hold, "n": n, "latest": latest}


if __name__ == "__main__":
    out = build_diffusion()
    m = out["meta"]
    if m["error"]:
        sys.exit(f"diffusion: {m['error']}")
    lt = out["latest"]
    print(f"{m['n_series']} series, {m['n_banks']} banks counted in {lt['month']}: "
          f"{len(lt['hiking'])} hiking · {len(lt['cutting'])} cutting · {len(lt['hold'])} hold; "
          f"index {out['index'][-1]} ({m['fetch_mode']})")
    if "--json" in sys.argv[1:]:
        print(json.dumps(out, indent=1, ensure_ascii=False))
