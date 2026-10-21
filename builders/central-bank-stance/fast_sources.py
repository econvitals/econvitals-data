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

  US  Federal Reserve      FRED DFEDTARU/DFEDTARL midpoint   same day   needs FRED_API_KEY
  XM  ECB                  ECB Data Portal, deposit facility same day   keyless
  GB  Bank of England      Bank Rate (IADB IUDBEDR)          ~1 day     keyless
  CA  Bank of Canada       Valet V39079, target overnight    ~1 day     keyless
  IN  Reserve Bank of India  rbi.org.in home page + MPC      same day   keyless; HTML, not a feed
                             resolution press releases

The RBI one is different in kind: the RBI publishes no machine-readable series (its data
files sit behind a bot wall, DBIE is an encrypted gateway), so `rbi_in` reads the bank's
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
import urllib.parse
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


# ------------------------------------------------------------------------ India
RBI_HOME = "https://www.rbi.org.in/"
RBI_LISTING = "https://www.rbi.org.in/scripts/Annualpolicy.aspx"
RBI_RATE_RANGE = (0.0, 20.0)   # a repo rate outside this is a parse gone wrong, not a policy
RBI_MAX_YEARS_BACK = 3         # fiscal-year listings to walk back for an anchor at/before `start`
_RBI_UP = ("increase", "raise", "hike")
_RBI_DOWN = ("reduce", "cut", "lower")


def _text(html_: str) -> str:
    """Tags out, entities in, whitespace collapsed — the regexes below read prose."""
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", html_)))


def _rbi_rate(v) -> float:
    r = float(v)
    lo, hi = RBI_RATE_RANGE
    if not (lo < r < hi):
        raise ValueError(f"RBI repo rate {r} is outside {lo}-{hi}: a parse gone wrong")
    return r


def _rbi_date(s: str) -> str:
    return datetime.strptime(s.strip(), "%b %d, %Y").date().isoformat()


def parse_rbi_home(html_: str) -> float:
    """Today's policy repo rate from the home page's Current Rates box ('Policy Repo Rate : 5.50%')."""
    m = re.search(r"Policy Repo Rate\s*:\s*(\d+(?:\.\d+)?)\s*%", _text(html_))
    if not m:
        raise ValueError("RBI home page: no 'Policy Repo Rate : X%' text")
    return _rbi_rate(m.group(1))


def parse_rbi_listing(html_: str) -> list[tuple[str, str]]:
    """(iso date, press-release URL) of every 'Resolution of the Monetary Policy Committee' on a
    Monetary Policy Statements listing (one fiscal year), newest first."""
    out = []
    parts = re.split(r'<td class="tableheader"><b>([A-Z][a-z]{2} \d{2}, \d{4})</b></td>', html_)
    for i in range(1, len(parts), 2):
        m = re.search(r'Resolution of the Monetary Policy Committee[^<]*<BR>\s*<DIR>\s*<li>'
                      r'<a href="([^"]+)"', parts[i + 1], re.I)
        if m:
            out.append((_rbi_date(parts[i]), html.unescape(m.group(1))))
    return out


def parse_rbi_listing_form(html_: str) -> tuple[dict, int]:
    """The listing's hidden form fields plus its invisible submit button, and the fiscal-year id
    at the top of its year tree (2027 = 2026-27). Earlier years come back from a plain POST of
    these fields with `hdnYear` set — what the page's own GetYear() does."""
    fields = {}
    for m in re.finditer(r'<input[^>]+type="hidden"[^>]*>', html_):
        t = m.group(0)
        n, v = re.search(r'name="([^"]+)"', t), re.search(r'value="([^"]*)"', t)
        if n:
            fields[n.group(1)] = html.unescape(v.group(1)) if v else ""
    b = re.search(r'<input[^>]+id="btn"[^>]*>', html_)
    if b:
        n, v = re.search(r'name="([^"]+)"', b.group(0)), re.search(r'value="([^"]*)"', b.group(0))
        if n:
            fields[n.group(1)] = v.group(1) if v else ""
    y = re.search(r'class="year_tree"[^>]*\bid="(\d{4})"', html_)
    if not y or "__VIEWSTATE" not in fields:
        raise ValueError("RBI listing: no year tree or no form state")
    return fields, int(y.group(1))


def parse_rbi_resolution(html_: str) -> tuple[str, float, float | None]:
    """(iso decision date, repo rate decided, repo rate it replaced) from one MPC resolution
    press release. The RBI's moves take effect the day they are announced, which is how BIS
    dates them too. The rate it replaced is None when the text names a move but not its size."""
    t = _text(html_)
    d = re.search(r"Date\s*:\s*([A-Z][a-z]{2} \d{2}, \d{4})", t)
    if not d:
        raise ValueError("RBI resolution: no 'Date :' line")
    m = re.search(r"policy repo rate\b.{0,200}?\b(?:to|at)\s+(\d+(?:\.\d+)?)\s*per\s*cent", t, re.I)
    if not m:
        raise ValueError("RBI resolution: no 'policy repo rate ... X per cent' sentence")
    rate = _rbi_rate(m.group(1))
    lead = t[max(0, m.start() - 90):m.start()].lower()
    if "unchanged" in t[m.start():m.end()].lower() or re.search(r"\bkeep\b|\bretain", lead):
        return _rbi_date(d.group(1)), rate, rate
    direction = (1 if any(w in lead for w in _RBI_UP) else -1 if any(w in lead for w in _RBI_DOWN) else 0)
    size = re.search(r"policy repo rate\b[^.]{0,120}?\bby\s+(\d+)\s*(?:bps|basis points)", t, re.I)
    if direction and size:
        return _rbi_date(d.group(1)), rate, _rbi_rate(rate - direction * int(size.group(1)) / 100)
    return _rbi_date(d.group(1)), rate, (rate if not direction else None)


def _post(url: str, fields: dict) -> str:
    data = urllib.parse.urlencode(fields).encode()
    req = urllib.request.Request(url, data=data, headers={
        **UA, "Content-Type": "application/x-www-form-urlencoded", "Referer": url})
    return urllib.request.urlopen(req, timeout=TIMEOUT).read().decode("utf-8", "replace")


def rbi_in(start: str, fetch=None, post=None) -> list[tuple[str, float]]:
    """Policy repo rate as the step series of MPC decisions back to `start`, closed with today's
    reading from the home page — the same basis as BIS's D.IN (decision day = effective day).

    Reads: the home page (today's value), the Monetary Policy Statements listing (which press
    releases are MPC resolutions, and when), walking back a fiscal year at a time until a decision
    dated on or before `start` is in hand, and those resolutions (rate decided, size of the move).
    The home page must agree with the latest resolution or the whole series is refused — the two
    pages are the cross-check on each other. One anchor point is added the day before the oldest
    decision, at the rate that decision says it replaced, so BIS's last date can be matched even
    when it falls between meetings. `fetch` and `post` are injectable for tests.
    """
    fetch, post = fetch or _get, post or _post
    now = parse_rbi_home(fetch(RBI_HOME))
    listing = fetch(RBI_LISTING)
    items = parse_rbi_listing(listing)
    fields, fy = parse_rbi_listing_form(listing)
    back = 0
    while (not items or items[-1][0] > start) and back < RBI_MAX_YEARS_BACK:
        back += 1
        earlier = post(RBI_LISTING, {**fields, "hdnYear": str(fy - back)})
        items += parse_rbi_listing(earlier)
        fields = parse_rbi_listing_form(earlier)[0]
    decisions = []
    for _, url in items:               # newest first; stop once anchored at/before start
        d, rate, prev = parse_rbi_resolution(fetch(url))
        decisions.append((d, rate, prev))
        if d <= start:
            break
    if not decisions:
        raise ValueError("RBI listing: no MPC resolutions found")
    if abs(decisions[0][1] - now) > 0.011:
        raise ValueError(f"RBI home page shows {now} but the latest MPC resolution "
                         f"({decisions[0][0]}) decided {decisions[0][1]}; not splicing")
    series = [(d, r) for d, r, _ in decisions]
    oldest_d, _, oldest_prev = decisions[-1]
    if oldest_prev is not None:
        series.append(((date.fromisoformat(oldest_d) - timedelta(days=1)).isoformat(), oldest_prev))
    today = date.today().isoformat()
    if today > decisions[0][0]:
        series.append((today, now))
    return _clean(series)


FAST = {
    "US": ("FRED (Board of Governors) — federal funds target range", fed_us),
    "XM": ("ECB Data Portal — deposit facility rate", ecb_xm),
    "GB": ("Bank of England — Bank Rate (IUDBEDR)", boe_gb),
    "CA": ("Bank of Canada — target for the overnight rate", boc_ca),
    "IN": ("Reserve Bank of India — Policy Repo Rate (rbi.org.in home page + MPC resolutions)", rbi_in),
}


def lookback_start(days: int = 120) -> str:
    return (date.today() - timedelta(days=days)).isoformat()
