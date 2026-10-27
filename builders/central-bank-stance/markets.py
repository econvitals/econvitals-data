#!/usr/bin/env python3
"""
What markets price for each central bank's NEXT decision — free, keyless sources only.

The Stance Matrix's "next expected move" is a desk view typed into cb_matrix_config.yaml.
This module puts a market reading beside it, so the page can show what traders think and
the desk text can be checked against it. One call per bank, one dict back, never an
exception out of the module: a source that is down, changed shape or has no upcoming
event is reported as unavailable with the reason, and the other sources still serve.

  US  Kalshi               econvitals.org/api/fed-kalshi (the site's own relay of the
                           KXFEDDECISION contract: outcome probabilities, decision date)
      CME futures          data/fed-funds-path.json in this repo — the chartbook's
                           futures-implied monthly path (`implied_now`), read off GitHub
      Polymarket           the "Fed Decision in <Month>?" event — a cross-check on Kalshi
  XM  Polymarket           "ECB Interest Rates: <Month Year>"
  GB  Polymarket           "Bank of England decision in <Month>?"
      Bank of England      the Bank's own OIS curve (latest-yield-curve-data.zip): the
                           instantaneous forward overnight rate 3, 6, 12 and 24 months out
  IN  Polymarket           "Reserve Bank of India decision in <Month>"
  JP CA AU NZ CH           Polymarket, the "Global-Rates" listing (added 2026-10-26)
  AU  ASX                  the exchange's own RBA Rate Tracker (30-day interbank cash rate
                           futures): probability of no change vs a move at the next meeting
  XM GB IN JP CA AU CH     Kalshi's public trade API, read direct (no key — reads are open;
  SE NO NZ                 only trading is signed). The ONLY source for Sweden and Norway.
  CN                       no free source (Kalshi's China series has had no event since March
                           2026; Polymarket's one China market is a year-end question on the
                           reverse-repo rate, not the LPR) — the desks quote pricing instead

Until 2026-10-26 the docstring said eight banks had no free source. A sweep that day found
Polymarket listing five of them under a tag this module was not reading (`Global-Rates`, not
`interest-rates`) and Kalshi listing seven under series the relay does not touch.

Polymarket is read through its public Gamma API (no key). Its monthly decision events are
"negative risk" groups: one Yes/No market per bracket (50+ bps decrease, 25 bps decrease,
no change, 25 bps increase, 50+ bps increase), and the Yes prices across the brackets are
the probabilities, summing to about 1. The event for the NEAREST decision still ahead of
`today` is the one reported — never a past one, and never the December event while the
November one is open. The decision date is read from the event's own description
("scheduled for October 27-28, 2026" -> the last day named), with the event's end date as
the fallback. Note the Fed events are tagged `fomc` / `fed-rates`, NOT `interest-rates`
(checked 2026-10-25), and the newer banks' only under `Global-Rates` (2026-10-26), so three
tag listings are read and de-duplicated.

Kalshi's events are the same shape: one market per bracket ("Cut more than 25bps", "Cut
1-25bps", "Maintain current rate", "Hike 25bps" …), the probability is the bracket's mid
quote (last trade when the book is empty or wider than KALSHI_MAX_SPREAD). The nearest event
with a bracket still trading is reported, and its decision date is the markets' CLOSE time
(trading stops just before the announcement) — never the subtitle, which is written in U.S.
time ("Oct 29" for the Bank of Japan's Oct 30, "Nov 2" for the RBA's Nov 3) or says only
"In Oct 2026".

A bank with no event listed right now (between meetings, or a series that has gone quiet)
is recorded under `not_listed`, not `errors`: the page lists `errors` as "could not be
read", and "nobody has opened next month's market yet" is not a failure.

`thin` marks a Polymarket event with less than THIN_LIQUIDITY_USD of liquidity or less
than THIN_VOLUME_USD traded over its life — small enough that one trader moves the price
(the RBI and the second-month BoE events were both thin on 2026-10-25) — and a direct Kalshi
event with less than THIN_VOLUME_USD contracts traded or a leading bracket quoted wider than
KALSHI_MAX_SPREAD (Norway, 2026-10-26: 14k contracts, a 9-cent spread). The Fed relay, the
ASX futures and the OIS curve carry no such flag (deep markets, or not a bet).

The BoE OIS workbook needs `openpyxl` (the Action installs it); without it that one
source is reported unavailable and everything else works. Everything else is stdlib.

Read-only everywhere: this module places no order and signs nothing.
"""

from __future__ import annotations

import io
import json
import re
import sys
import urllib.request
import zipfile
from datetime import date, datetime, timezone

UA = {"User-Agent": "jfmacro-lab/1.0"}
TIMEOUT = 45

KALSHI_URL = "https://econvitals.org/api/fed-kalshi"
FED_FUNDS_PATH_URL = ("https://raw.githubusercontent.com/econvitals/econvitals-data/main/"
                      "data/fed-funds-path.json")
GAMMA_URL = "https://gamma-api.polymarket.com/events?closed=false&limit=100&tag_slug={tag}"
# `interest-rates` carries the ECB, BoE and RBI monthly events; the Fed's sit under `fomc`;
# `Global-Rates` carries all of those AND the BoJ, BoC, RBA, RBNZ and SNB (37 events on
# 2026-10-26, hence limit=100). The first two stay as a fallback if the tag is ever renamed.
POLYMARKET_TAGS = ("interest-rates", "fomc", "Global-Rates")
POLYMARKET_EVENT_URL = "https://polymarket.com/event/{slug}"
BOE_OIS_ZIP_URL = ("https://www.bankofengland.co.uk/-/media/boe/files/statistics/"
                   "yield-curves/latest-yield-curve-data.zip")
BOE_OIS_HORIZONS_MONTHS = (3, 6, 12, 24)
KALSHI_API_URL = ("https://api.elections.kalshi.com/trade-api/v2/events?series_ticker={series}"
                  "&status=open&with_nested_markets=true")
KALSHI_PAGE_URL = "https://kalshi.com/markets/{series}"
# Kalshi's central-bank decision series (checked live 2026-10-26). The Fed is read through the
# site's own relay instead (KXFEDDECISION, with its decision-date logic); dead series NOT to
# use: KXBOJDECISION, KXRBADECISION, KXECB, KXBOE, and KXCBDECISIONCHINA (no event since March).
KALSHI_SERIES = {
    "XM": "KXCBDECISIONEU", "GB": "KXCBDECISIONENGLAND", "IN": "KXCBDECISIONINDIA",
    "JP": "KXCBDECISIONJAPAN", "CA": "KXCBDECISIONCANADA", "AU": "KXCBDECISIONAUSTRALIA",
    "CH": "KXCBDSWISS", "SE": "KXCBDSWEDEN", "NO": "KXCBDNORWAY", "NZ": "KXCBDECISIONNZ",
}
# A quote wider than this is not a price: the bracket falls back to its last trade, and a
# leading bracket this wide marks the event thin.
KALSHI_MAX_SPREAD = 0.08
ASX_RBA_URL = "https://www.asx.com.au/content/dam/asx/data/market_exp.csv"   # JSON, despite the name
ASX_RBA_PAGE = ("https://www.asx.com.au/markets/trade-our-derivatives-market/futures-market/"
                "rba-rate-tracker")
ASX_MAX_AGE_DAYS = 10    # older than this, the tracker is stale (it updates every trading day)

# A Polymarket event below either line is `thin`: a few thousand dollars of depth is one
# trader's position, not a market view.
THIN_LIQUIDITY_USD = 10_000
THIN_VOLUME_USD = 25_000
# The bracket Yes prices should sum to ~1; outside this band the book is out of line and the
# source carries a note (the numbers are still reported, unnormalized).
PROB_SUM_TOL = 0.05

# How each bank's monthly decision event is titled on Polymarket. Case-insensitive: the same
# listing writes "Decision" for some banks and "decision" for others, and has changed it.
_PM = lambda pat: re.compile(pat, re.I)  # noqa: E731
POLYMARKET_TITLE = {
    "US": _PM(r"^Fed Decision in [A-Z][a-z]+\??$"),
    "XM": _PM(r"^ECB Interest Rates: [A-Z][a-z]+ \d{4}$"),
    "GB": _PM(r"^Bank of England decision in [A-Z][a-z]+\??$"),
    "IN": _PM(r"^Reserve Bank of India decision in [A-Z][a-z]+\??$"),
    "JP": _PM(r"^Bank of Japan decision in [A-Z][a-z]+\??$"),
    "CA": _PM(r"^Bank of Canada decision in [A-Z][a-z]+\??$"),
    "AU": _PM(r"^Reserve Bank of Australia decision in [A-Z][a-z]+\??$"),
    "NZ": _PM(r"^Reserve Bank of New Zealand decision in [A-Z][a-z]+\??$"),
    "CH": _PM(r"^Swiss National Bank decision in [A-Z][a-z]+\??$"),
}
NO_FREE_SOURCE = ("CN",)
ALL_CODES = ("US", "XM", "GB", "IN", "JP", "CA", "AU", "CH", "SE", "NO", "NZ", "CN")


class NotListed(ValueError):
    """The source works but has no event for this bank's next decision right now — reported
    under `not_listed`, never as a read failure."""

_MONTHS = {m: i for i, m in enumerate(
    ("January", "February", "March", "April", "May", "June", "July", "August",
     "September", "October", "November", "December"), start=1)}
_DATE_IN_TEXT = re.compile(
    r"\b(January|February|March|April|May|June|July|August|September|October|November|December)"
    r"\s+(\d{1,2})(?:\s*[-–]\s*(\d{1,2}))?,\s+(\d{4})")


def _get(url: str) -> bytes:
    return urllib.request.urlopen(
        urllib.request.Request(url, headers=UA), timeout=TIMEOUT).read()


def _now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _memo(fetch):
    """One network call per URL per run — all_markets reads the Gamma listings once for
    four banks."""
    seen: dict[str, bytes] = {}

    def f(url: str) -> bytes:
        if url not in seen:
            seen[url] = fetch(url)
        return seen[url]
    return f


def _num(v, default=0.0) -> float:
    """Gamma serves numbers as floats on events and as strings on markets."""
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _source(name: str, url: str, **kw) -> dict:
    s = {"source": name, "asof": _now_iso(), "decision_date": None, "p_cut": None,
         "p_hold": None, "p_hike": None, "outcomes": [], "implied_path": [], "thin": False,
         "url": url}
    s.update(kw)
    return s


def _probs(outcomes: list[dict]) -> dict:
    """p_cut / p_hold / p_hike from [{moves, prob}], rounded to 4 places."""
    cut = sum(o["prob"] for o in outcomes if o["moves"] < 0)
    hold = sum(o["prob"] for o in outcomes if o["moves"] == 0)
    hike = sum(o["prob"] for o in outcomes if o["moves"] > 0)
    return {"p_cut": round(cut, 4), "p_hold": round(hold, 4), "p_hike": round(hike, 4)}


# ------------------------------------------------------------------------- US: Kalshi
def parse_kalshi(body: bytes) -> dict:
    """The site's fed-kalshi relay -> one source dict. Raises on a relay that says it is
    unavailable or has no next meeting (market_for turns that into available: False)."""
    d = json.loads(body)
    if not d.get("available", True):
        raise ValueError(f"relay reports unavailable: {d.get('error') or 'no reason given'}")
    nm = d.get("next_meeting") or {}
    outcomes = [{"label": str(o.get("label", "")), "prob": float(o["prob"]), "moves": int(o["moves"])}
                for o in nm.get("outcomes", []) if o.get("prob") is not None]
    if not outcomes or not nm.get("decision_date"):
        raise ValueError("relay has no next_meeting outcomes")
    p = _probs(outcomes)
    if nm.get("p_hold") is not None:
        p["p_hold"] = round(float(nm["p_hold"]), 4)
    if nm.get("p_hike") is not None:
        p["p_hike"] = round(float(nm["p_hike"]), 4)
    return _source("Kalshi", KALSHI_URL, asof=str(d.get("asof") or nm.get("asof") or _now_iso()),
                   decision_date=str(nm["decision_date"]), outcomes=outcomes,
                   ticker=nm.get("ticker"), **p)


# ------------------------------------------------------------- US: futures-implied path
def parse_fed_funds_path(body: bytes, today: date) -> dict:
    """The chartbook's fed-funds-path chart data -> the futures-implied rate by month, from
    the current month on (`implied_now`), plus the last actual."""
    d = json.loads(body)
    rows = d.get("data") or []
    cols = d.get("columns") or []
    if "implied_now" not in cols:
        raise ValueError("fed-funds-path.json has no implied_now series")
    this_month = today.strftime("%Y-%m")
    path = [{"date": r["date"][:7], "rate": float(r["implied_now"])}
            for r in rows if r.get("implied_now") is not None and r.get("date", "")[:7] >= this_month]
    if not path:
        raise ValueError("fed-funds-path.json: no implied_now values from this month on")
    actual = [(r["date"][:7], float(r["actual"])) for r in rows if r.get("actual") is not None]
    return _source("CME futures (chartbook)", FED_FUNDS_PATH_URL, implied_path=path,
                   actual_now=actual[-1][1] if actual else None,
                   actual_through=actual[-1][0] if actual else None,
                   note=str(d.get("source") or ""))


# ------------------------------------------------------------------------ Polymarket
def parse_polymarket_events(body: bytes) -> list[dict]:
    ev = json.loads(body)
    if not isinstance(ev, list):
        raise ValueError("Gamma /events did not return a list")
    return ev


def polymarket_outcome(market: dict):
    """One bracket market -> {label, prob, moves}, or None for a market that is not a
    decision bracket. The bracket name is `groupItemTitle` ("25 bps decrease"), with the
    question text as the fallback; the probability is the Yes price."""
    name = str(market.get("groupItemTitle") or market.get("question") or "").lower()
    if re.search(r"\bno\s+change\b", name):
        moves = 0
    else:
        size = re.search(r"(\d+)(\+?)\s*bps?\b", name)
        up = re.search(r"\b(increas|hik|rais)", name)
        dn = re.search(r"\b(decreas|cut|lower)", name)
        if not size or bool(up) == bool(dn):
            return None
        n = int(size.group(1)) // 25 or 1
        if size.group(2):       # "50+ bps" = that size or more
            n = max(n, 2)
        moves = n if up else -n
    outcomes = market.get("outcomes")
    prices = market.get("outcomePrices")
    outcomes = json.loads(outcomes) if isinstance(outcomes, str) else (outcomes or [])
    prices = json.loads(prices) if isinstance(prices, str) else (prices or [])
    try:
        i = [str(o).lower() for o in outcomes].index("yes")
        prob = float(prices[i])
    except (ValueError, IndexError, TypeError):
        return None
    if moves == 0:
        label = "Hold"
    elif abs(moves) == 1:
        label = ("Hike" if moves > 0 else "Cut") + " 25 bp"
    else:
        label = ("Hike" if moves > 0 else "Cut") + " >25 bp"
    return {"label": label, "prob": prob, "moves": moves}


def polymarket_decision_date(event: dict) -> str | None:
    """The decision day the event's description names ("scheduled for October 27-28, 2026"
    -> 2026-10-28; "to be announced on December 4, 2026" -> 2026-12-04), else the event's
    end date."""
    m = _DATE_IN_TEXT.search(event.get("description") or "")
    if m:
        day = int(m.group(3) or m.group(2))
        try:
            return date(int(m.group(4)), _MONTHS[m.group(1)], day).isoformat()
        except ValueError:
            pass
    end = str(event.get("endDate") or "")[:10]
    return end or None


def polymarket_event_for(events: list[dict], code: str, today: date):
    """The bank's decision event with the nearest decision date on or after `today`, with
    its decision date — (None, None) when none is ahead."""
    pat = POLYMARKET_TITLE.get(code)
    if not pat:
        return None, None
    best = None
    for e in events:
        if not pat.match(str(e.get("title") or "").strip()) or e.get("closed"):
            continue
        dd = polymarket_decision_date(e)
        if not dd or dd < today.isoformat():
            continue
        if best is None or dd < best[1]:
            best = (e, dd)
    return best if best else (None, None)


def polymarket_source(events: list[dict], code: str, today: date) -> dict:
    event, dd = polymarket_event_for(events, code, today)
    if event is None:
        raise NotListed(f"no upcoming Polymarket decision event for {code} as of {today}")
    outcomes = [o for o in (polymarket_outcome(m) for m in event.get("markets", [])) if o]
    if not outcomes:
        raise ValueError(f"Polymarket event {event.get('slug')} has no bracket markets")
    outcomes.sort(key=lambda o: o["moves"])
    liq, vol = _num(event.get("liquidity")), _num(event.get("volume"))
    total = sum(o["prob"] for o in outcomes)
    src = _source("Polymarket", POLYMARKET_EVENT_URL.format(slug=event.get("slug", "")),
                  decision_date=dd, outcomes=outcomes, event_title=str(event.get("title")),
                  liquidity_usd=round(liq), volume_usd=round(vol), prob_sum=round(total, 4),
                  thin=(liq < THIN_LIQUIDITY_USD or vol < THIN_VOLUME_USD), **_probs(outcomes))
    if abs(total - 1) > PROB_SUM_TOL:
        src["note"] = f"bracket prices sum to {total:.3f}, not 1"
    return src


# ------------------------------------------------------------- Kalshi, read direct
def kalshi_moves(label: str):
    """A Kalshi bracket's name -> moves in 25 bp steps, or None. "Maintain current rate" /
    "Maintains rate" 0; "Cut 1-25bps" / "Cut 25bps" -1; "Cut more than 25bps" / "Cut >25bps" -2;
    the same for "Hike"."""
    name = str(label or "").lower()
    if re.search(r"\b(maintain|hold|no change|unchanged)", name):
        return 0
    up = re.search(r"\b(hike|increase|raise)", name)
    dn = re.search(r"\b(cut|decrease|lower)", name)
    if bool(up) == bool(dn):
        return None
    if re.search(r"more than|>|\+|or more", name):
        n = 2
    else:
        sizes = [int(x) for x in re.findall(r"(\d+)\s*bps?", name)]
        n = max(1, max(sizes) // 25) if sizes else 1
    return n if up else -n


def _kalshi_price(m: dict):
    """The bracket's probability: the mid quote, or the last trade when there is no ask or
    the book is wider than KALSHI_MAX_SPREAD."""
    bid, ask, last = (_num(m.get(k), None) for k in ("yes_bid_dollars", "yes_ask_dollars",
                                                       "last_price_dollars"))
    if bid is not None and ask is not None and ask > 0 and ask - bid <= KALSHI_MAX_SPREAD:
        return round((bid + ask) / 2, 4), ask - bid
    if last is not None:
        return last, (ask - bid if bid is not None and ask is not None and ask > 0 else None)
    return None, None


def kalshi_source(body: bytes, code: str, today: date) -> dict:
    """Kalshi's /events?series_ticker=…&with_nested_markets=true -> one source dict for the
    nearest event with a bracket still trading. Raises NotListed when no such event is open."""
    series = KALSHI_SERIES[code]
    d = json.loads(body)
    best = None
    for e in d.get("events") or []:
        live = [m for m in e.get("markets") or [] if str(m.get("status", "active")) == "active"]
        closes = sorted(str(m.get("close_time") or "")[:10] for m in live if m.get("close_time"))
        if not closes or closes[0] < today.isoformat():
            continue
        if best is None or closes[0] < best[1]:
            best = (e, closes[0], live)
    if best is None:
        raise NotListed(f"no open Kalshi {series} event as of {today}")
    event, dd, live = best
    outcomes, volume, lead_spread, lead_p = [], 0.0, None, -1.0
    for m in live:
        moves = kalshi_moves(m.get("yes_sub_title") or m.get("title"))
        prob, spread = _kalshi_price(m)
        volume += _num(m.get("volume_fp"))
        if moves is None or prob is None:
            continue
        label = "Hold" if moves == 0 else ("Hike" if moves > 0 else "Cut") + (" 25 bp" if abs(moves) == 1 else " >25 bp")
        outcomes.append({"label": label, "prob": prob, "moves": moves})
        if prob > lead_p:
            lead_p, lead_spread = prob, spread
    if not outcomes:
        raise ValueError(f"Kalshi {event.get('event_ticker')} has no bracket markets")
    outcomes.sort(key=lambda o: o["moves"])
    total = sum(o["prob"] for o in outcomes)
    thin = volume < THIN_VOLUME_USD or lead_spread is None or lead_spread > KALSHI_MAX_SPREAD
    src = _source("Kalshi", KALSHI_PAGE_URL.format(series=series.lower()), decision_date=dd,
                  outcomes=outcomes, ticker=event.get("event_ticker"),
                  event_title=str(event.get("title") or ""), volume_contracts=round(volume),
                  prob_sum=round(total, 4), thin=thin, **_probs(outcomes))
    if abs(total - 1) > PROB_SUM_TOL:
        src["note"] = f"bracket prices sum to {total:.3f}, not 1"
    return src


# ------------------------------------------------------------- AU: ASX RBA Rate Tracker
def parse_asx_rba(body: bytes, today: date) -> dict:
    """The ASX RBA Rate Tracker's data file -> one source dict: the newest settlement day's
    probability of no change vs a move of `Ftre_Cash_Rate_Change` at the next meeting, priced
    off the 30-day interbank cash rate futures. Raises when the file is stale or gives no
    direction."""
    d = json.loads(body)
    days = [x for x in d.get("days") or [] if x.get("Stlmnt_Dt")]
    if not days:
        raise ValueError("ASX tracker has no settlement days")
    newest = max(days, key=lambda x: x["Stlmnt_Dt"])
    asof = date.fromisoformat(str(newest["Stlmnt_Dt"])[:10])
    if (today - asof).days > ASX_MAX_AGE_DAYS:
        raise ValueError(f"ASX tracker stale: newest settlement {asof}")
    change = _num(d.get("Ftre_Cash_Rate_Change"), None)
    if not change:
        raise ValueError("ASX tracker names no direction for the move it prices")
    p_move = _num(newest.get("Prob_Change")) / 100
    p_hold = _num(newest.get("Prob_No_Change")) / 100
    moves = (1 if change > 0 else -1) * max(1, round(abs(change) / 0.25))
    label = ("Hike" if moves > 0 else "Cut") + (" 25 bp" if abs(moves) == 1 else " >25 bp")
    outcomes = sorted([{"label": "Hold", "prob": round(p_hold, 4), "moves": 0},
                       {"label": label, "prob": round(p_move, 4), "moves": moves}],
                      key=lambda o: o["moves"])
    return _source("ASX futures", ASX_RBA_PAGE, asof=asof.isoformat(), outcomes=outcomes,
                   expected_rate=_num(d.get("Ftre_Cash_Rate"), None),
                   note="ASX RBA Rate Tracker: 30-day interbank cash rate futures, at the close",
                   **_probs(outcomes))


# -------------------------------------------------------------- GB: Bank of England OIS
def _add_months(d: date, n: int) -> str:
    y, m = divmod(d.month - 1 + n, 12)
    return f"{d.year + y:04d}-{m + 1:02d}"


def parse_boe_ois(zip_bytes: bytes, horizons=BOE_OIS_HORIZONS_MONTHS) -> dict:
    """The Bank's yield-curve zip -> the instantaneous OIS forward rate `horizons` months
    ahead on the latest curve date (sheet "1. fwds, short end" of the OIS workbook: one row
    per day, one column per month 1..60)."""
    try:
        import openpyxl  # noqa: PLC0415 — optional; the Action installs it
    except ImportError as e:
        raise RuntimeError("openpyxl not installed; cannot read the OIS workbook") from e
    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as z:
        names = [n for n in z.namelist() if "ois" in n.lower() and n.lower().endswith(".xlsx")]
        if not names:
            raise ValueError(f"no OIS workbook in the zip: {z.namelist()}")
        wb = openpyxl.load_workbook(io.BytesIO(z.read(names[0])), read_only=True, data_only=True)
    sheet = next((s for s in wb.sheetnames if "fwd" in s.lower() and "short" in s.lower()), None)
    if not sheet:
        raise ValueError(f"no short-end forward sheet in {names[0]}: {wb.sheetnames}")
    months: list[int] = []
    latest = None
    for row in wb[sheet].iter_rows(values_only=True):
        head = row[0]
        if isinstance(head, str) and head.strip().lower().startswith("months"):
            months = [int(round(float(v))) if isinstance(v, (int, float)) else None for v in row[1:]]
        elif isinstance(head, datetime) and months and any(isinstance(v, (int, float)) for v in row[1:]):
            if latest is None or head > latest[0]:
                latest = (head, row[1:])
    if not months or latest is None:
        raise ValueError(f"sheet {sheet!r}: no months header or no dated rows")
    curve_date, values = latest
    col = {m: i for i, m in enumerate(months) if m is not None}
    path = []
    for h in horizons:
        if h not in col or not isinstance(values[col[h]], (int, float)):
            raise ValueError(f"sheet {sheet!r}: no {h}-month forward on {curve_date.date()}")
        path.append({"date": _add_months(curve_date.date(), h), "months": h,
                     "rate": round(float(values[col[h]]), 3)})
    return _source("Bank of England OIS curve", BOE_OIS_ZIP_URL, asof=curve_date.date().isoformat(),
                   curve_date=curve_date.date().isoformat(), implied_path=path,
                   note="instantaneous OIS forward rate (sheet '%s'); the curve date is the "
                        "latest in the Bank's current-month file" % sheet)


# ------------------------------------------------------------------------------ API
def _try(name: str, fn, errors: dict):
    """Run one source; a failure of any kind is recorded by name, never raised."""
    try:
        return fn()
    except Exception as e:  # noqa: BLE001 — a failing source must not take the row down
        errors[name] = e        # market_for turns it into text (or moves a NotListed aside)
        return None


def market_for(code: str, fetch=None, today: date | None = None) -> dict:
    """What markets price for one bank's next decision.

    {"code", "available": bool, "sources": [source dicts that served], "errors": {source
    name: why it did not}, "error": None | str}. A source dict carries "source", "asof",
    "decision_date", "p_cut"/"p_hold"/"p_hike", "outcomes" [{label, prob, moves}],
    "implied_path" [{date "YYYY-MM", rate}], "thin", "url" (plus a few source-specific
    extras). `fetch(url) -> bytes` and `today` are injectable so tests run offline and
    pin the day. Never raises.
    """
    code = (code or "").upper()
    fetch = fetch or _get
    today = today or datetime.now(timezone.utc).date()
    out = {"code": code, "available": False, "sources": [], "errors": {}, "not_listed": {},
           "error": None}
    if code in NO_FREE_SOURCE:
        out["error"] = "no free source"
        return out
    if code not in ALL_CODES:
        out["error"] = f"unknown bank code {code!r}"
        return out
    errors = out["errors"]
    plan = []
    if code == "US":
        plan.append(("Kalshi", lambda: parse_kalshi(fetch(KALSHI_URL))))
        plan.append(("CME futures (chartbook)",
                     lambda: parse_fed_funds_path(fetch(FED_FUNDS_PATH_URL), today)))
    if code == "AU":
        plan.append(("ASX futures", lambda: parse_asx_rba(fetch(ASX_RBA_URL), today)))

    def poly():
        events, failed = [], []
        for tag in POLYMARKET_TAGS:
            try:
                events += parse_polymarket_events(fetch(GAMMA_URL.format(tag=tag)))
            except Exception as e:  # noqa: BLE001 — one listing down must not hide the other
                failed.append(f"{tag}: {type(e).__name__}: {e}")
        if not events:
            raise RuntimeError("Gamma listings unreachable — " + "; ".join(failed))
        seen, uniq = set(), []
        for e in events:
            if e.get("id") not in seen:
                seen.add(e.get("id"))
                uniq.append(e)
        return polymarket_source(uniq, code, today)
    if code in POLYMARKET_TITLE:
        plan.append(("Polymarket", poly))
    if code in KALSHI_SERIES:
        plan.append(("Kalshi", lambda: kalshi_source(
            fetch(KALSHI_API_URL.format(series=KALSHI_SERIES[code])), code, today)))
    if code == "GB":
        plan.append(("Bank of England OIS curve", lambda: parse_boe_ois(fetch(BOE_OIS_ZIP_URL))))
    for name, fn in plan:
        src = _try(name, fn, errors)
        if src is not None:
            out["sources"].append(src)
        elif isinstance(errors.get(name), NotListed):
            out["not_listed"][name] = str(errors.pop(name))
        elif name in errors:
            errors[name] = f"{type(errors[name]).__name__}: {errors[name]}"
    out["available"] = bool(out["sources"])
    if not out["available"]:
        why = {**errors, **out["not_listed"]}
        out["error"] = "; ".join(f"{k}: {v}" for k, v in why.items()) or "no source served"
    return out


def all_markets(fetch=None, today: date | None = None) -> dict[str, dict]:
    """market_for every bank in the matrix, {code: dict}. Shares one fetch per URL across
    banks and never raises — a bank whose call blows up gets available: False."""
    fetch = _memo(fetch or _get)
    out = {}
    for code in ALL_CODES:
        try:
            out[code] = market_for(code, fetch=fetch, today=today)
        except Exception as e:  # noqa: BLE001
            out[code] = {"code": code, "available": False, "sources": [], "errors": {},
                         "not_listed": {}, "error": f"{type(e).__name__}: {e}"}
    return out


if __name__ == "__main__":
    res = all_markets()
    json.dump(res, sys.stdout, indent=1)
    print()
