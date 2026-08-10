#!/usr/bin/env python3
"""
Build data.json for the Risk Monitor Lab dashboard.

Risk Monitor is two visually opposed halves:
  * STRESS  — fast-moving, market-priced indicators ("how scared is everyone
              right now"): volatility, credit spreads, financial conditions,
              recession and inflation signals.
  * VULNERABILITIES — slow-moving valuation and balance-sheet measures ("how
              much damage would a shock do"): housing valuation, household and
              corporate leverage, funding stress.

They often move in opposite directions (calm markets are when leverage and
valuations build — the volatility paradox), so the page's job is to make that
tension legible. A small regime quadrant on top places us in the 2x2 of
Stress (low/high) x Vulnerability (low/high); the dangerous cell is
low-stress / high-vulnerability.

Launch set = the automatable core: every strip below is a pure FRED pull (a few
are simple ratios computed from two FRED series). The fragile scrapes (Shiller
CAPE/ERP, FDIC capital, NY Fed files, UMich, GPR, policy-uncertainty) and the
CFTC basis-trade / Treasury FiscalData additions come in a later hardening pass.

Core visual unit: the PercentileStrip. For each indicator we compute, over its
full available history:
  * the current value's percentile rank, oriented so rightward = riskier
    (a `higher_is_riskier` flag inverts equity-risk-premium-style series);
  * a "ghost" value from ~one month prior (prior quarter for quarterly series)
    and its percentile, for the delta marker;
  * a "biggest mover" flag when |delta percentile| >= 5;
  * a short-history flag when the series has < 10 years of data, so a strip is
    honest about a thin percentile;
  * a ~13-point monthly sparkline.

No wall-clock timestamp is written into data.json (only real data dates), so a
no-op run produces no diff and the updater makes no spurious commit.

Pure Python standard library (urllib, json) — no pip installs — so it runs
identically on a laptop and in GitHub Actions.

Run:  python3 fetch_data.py
"""
import json, os, re, sys, time, urllib.parse, urllib.request
from bisect import bisect_right
from datetime import date, timedelta
from pathlib import Path
from statistics import median

HERE = Path(__file__).resolve().parent
REPO = HERE.parent.parent  # builders/risk-monitor/ -> repo root
OUT = REPO / "lab" / "risk-monitor" / "data.json"
FRED = "https://api.stlouisfed.org/fred/series/observations"

# Colorblind-safe risk ramp (low -> high). Blue->amber->red avoids the red/green
# confusion of RdYlGn. Canonical home is tokens.yaml (promote via chartbook-edit
# clone); mirrored here and in strip.js for the front-end.
RAMP = ["#2C6FB5", "#F2C200", "#B01B1B"]

SHORT_HISTORY_YEARS = 10          # below this, a percentile strip is flagged thin
MOVER_THRESHOLD = 5.0             # |delta percentile| >= this => "biggest mover"


# ----------------------------------------------------------------------------- creds
def fred_key():
    k = os.environ.get("FRED_API_KEY")
    if k:
        return k.strip()
    envp = Path.home() / ".config" / "macro-dashboard" / ".env"
    if envp.exists():
        for line in envp.read_text().splitlines():
            line = line.strip()
            if line.startswith("FRED_API_KEY"):
                return line.split("=", 1)[1].strip().strip('"').strip("'")
    sys.exit("FRED_API_KEY not found (env or ~/.config/macro-dashboard/.env).")


KEY = fred_key()


# ----------------------------------------------------------------------------- fetch
def fred_raw(code, start="1900-01-01"):
    """Return list of (date, value:float) sorted ascending for a FRED series."""
    q = urllib.parse.urlencode(
        {"series_id": code, "api_key": KEY, "file_type": "json",
         "observation_start": start})
    url = f"{FRED}?{q}"
    for attempt in range(4):
        try:
            with urllib.request.urlopen(url, timeout=30) as r:
                obs = json.load(r)["observations"]
            out = []
            for o in obs:
                v = o["value"]
                if v in (".", "", None):
                    continue
                d = date.fromisoformat(o["date"])
                out.append((d, float(v)))
            if not out:
                raise RuntimeError(f"{code}: no observations")
            out.sort()
            return out
        except Exception as e:  # noqa
            if attempt == 3:
                raise
            time.sleep(1.5 * (attempt + 1))


_CACHE = {}


def series(code):
    if code not in _CACHE:
        _CACHE[code] = fred_raw(code)
    return list(_CACHE[code])


# ----------------------------------------------------------------------------- computed series
def _by_month(rows):
    """Collapse (date,val) rows to {('YYYY-MM'): last value in month}."""
    m = {}
    for d, v in rows:
        m[f"{d.year:04d}-{d.month:02d}"] = (d, v)  # last wins (rows sorted asc)
    return m


def price_to_rent():
    """Case-Shiller home price / CPI rent, both indexed to the first common month."""
    home = _by_month(series("CSUSHPINSA"))
    rent = _by_month(series("CUUR0000SEHA"))
    keys = sorted(set(home) & set(rent))
    h0, r0 = home[keys[0]][1], rent[keys[0]][1]
    out = []
    for k in keys:
        d = max(home[k][0], rent[k][0])
        val = (home[k][1] / h0) / (rent[k][1] / r0) * 100.0
        out.append((d, val))
    return out


def corp_debt_gdp():
    """Nonfinancial corporate debt (millions) as a share of GDP (billions)."""
    debt = {d: v for d, v in series("BCNSDODNS")}
    gdp = {d: v for d, v in series("GDP")}
    out = []
    for d in sorted(set(debt) & set(gdp)):
        out.append((d, (debt[d] / 1000.0) / gdp[d] * 100.0))
    return out


def sofr_iorb():
    """SOFR minus interest on reserve balances, in basis points (daily)."""
    sofr = {d: v for d, v in series("SOFR")}
    iorb = {d: v for d, v in series("IORB")}
    out = []
    for d in sorted(set(sofr) & set(iorb)):
        out.append((d, (sofr[d] - iorb[d]) * 100.0))
    return out


def equity_mktcap_gdp():
    """Nonfinancial corporate equities (millions) as a share of GDP (billions)."""
    eq = {d: v for d, v in series("NCBEILQ027S")}
    gdp = {d: v for d, v in series("GDP")}
    out = []
    for d in sorted(set(eq) & set(gdp)):
        out.append((d, (eq[d] / 1000.0) / gdp[d] * 100.0))
    return out


def fiscal_interest_receipts():
    """Federal interest payments as a share of federal receipts (percent)."""
    intr = {d: v for d, v in series("A091RC1Q027SBEA")}
    rec = {d: v for d, v in series("FGRECPT")}
    out = []
    for d in sorted(set(intr) & set(rec)):
        out.append((d, intr[d] / rec[d] * 100.0))
    return out


COMPUTED = {
    "price_to_rent": price_to_rent,
    "corp_debt_gdp": corp_debt_gdp,
    "sofr_iorb": sofr_iorb,
    "equity_mktcap_gdp": equity_mktcap_gdp,
    "fiscal_interest_receipts": fiscal_interest_receipts,
}


# ----------------------------------------------------------------------------- percentile strip
def pct_rank(sorted_vals, x):
    """Percentile rank (0-100) of x within a sorted list of values."""
    return 100.0 * bisect_right(sorted_vals, x) / len(sorted_vals)


def ghost_index(dates, days_back=28):
    """Index of the latest observation on/before (last_date - days_back)."""
    target = dates[-1] - timedelta(days=days_back)
    lo = None
    for i, d in enumerate(dates):
        if d <= target:
            lo = i
        else:
            break
    return lo


def monthly_spark(rows, n=13):
    """Last n monthly (last-value-in-month) points as [['YYYY-MM', val], ...]."""
    m = {}
    for d, v in rows:
        m[f"{d.year:04d}-{d.month:02d}"] = v
    keys = sorted(m)[-n:]
    return [[k, round(m[k], 4)] for k in keys]


def fmt_value(v, unit, decimals, scale):
    x = v / scale
    if unit == "k":
        return f"{x:,.{decimals}f}k"
    s = f"{x:,.{decimals}f}"
    suffix = {"%": "%", "pp": " pp", "bps": " bps",
              "% of GDP": "%", "index": "", "": ""}.get(unit, "")
    return s + suffix


def asof_label(d, freq):
    if freq == "quarterly":
        return f"{d.year}Q{(d.month - 1) // 3 + 1}"
    if freq in ("monthly", "quarterly"):
        return d.strftime("%b %Y")
    return d.isoformat()


def build_strip(cfg):
    if "compute" in cfg:
        rows = COMPUTED[cfg["compute"]]()
        codes = cfg["fred"]
    else:
        rows = series(cfg["fred"][0])
        codes = cfg["fred"]
    dates = [d for d, _ in rows]
    vals = [v for _, v in rows]
    sorted_vals = sorted(vals)

    higher = cfg["higher_is_riskier"]
    def risk_pct(x):
        raw = pct_rank(sorted_vals, x)
        return raw if higher else 100.0 - raw

    cur_v = vals[-1]
    cur_pct = risk_pct(cur_v)

    gi = ghost_index(dates)
    if gi is not None and gi != len(dates) - 1:
        prev_v = vals[gi]
        prev_pct = risk_pct(prev_v)
        prev_asof = dates[gi].isoformat()
        delta = cur_pct - prev_pct
    else:
        prev_v = prev_pct = prev_asof = delta = None

    span_years = (dates[-1] - dates[0]).days / 365.25
    scale = cfg.get("scale", 1)
    dec = cfg["decimals"]
    unit = cfg["unit"]

    return {
        "id": cfg["id"], "label": cfg["label"], "group": cfg["group"],
        "fred": codes, "unit": unit, "freq": cfg["freq"],
        "higher_is_riskier": higher,
        "value": round(cur_v, 4), "value_fmt": fmt_value(cur_v, unit, dec, scale),
        "asof": dates[-1].isoformat(), "asof_label": asof_label(dates[-1], cfg["freq"]),
        "stale": cfg["freq"] in ("quarterly",),
        "pct": round(cur_pct, 1),
        "prev_value": None if prev_v is None else round(prev_v, 4),
        "prev_value_fmt": None if prev_v is None else fmt_value(prev_v, unit, dec, scale),
        "prev_pct": None if prev_pct is None else round(prev_pct, 1),
        "prev_asof": prev_asof,
        "delta_pct": None if delta is None else round(delta, 1),
        "mover": bool(delta is not None and abs(delta) >= MOVER_THRESHOLD),
        "window_start": dates[0].isoformat()[:7],
        "window_years": round(span_years, 1),
        "short_history": span_years < SHORT_HISTORY_YEARS,
        "spark": monthly_spark(rows),
        "note": cfg["note"],
    }


# ----------------------------------------------------------------------------- layout
# panel: stress (fast) | vulnerabilities (slow).  group: display subheading.
INDICATORS = [
    # -- STRESS -------------------------------------------------------------- recession
    {"panel": "stress", "group": "Recession risk", "id": "term-spread",
     "label": "Yield-curve slope (10y − 3m)", "fred": ["T10Y3M"], "freq": "daily",
     "unit": "pp", "decimals": 2, "higher_is_riskier": False,
     "note": "Treasury term spread; a negative (inverted) curve has preceded every "
             "modern recession, so a lower value is riskier."},
    {"panel": "stress", "group": "Recession risk", "id": "sahm",
     "label": "Sahm rule (real-time)", "fred": ["SAHMREALTIME"], "freq": "monthly",
     "unit": "pp", "decimals": 2, "higher_is_riskier": True,
     "note": "Rise in the unemployment rate off its recent low; 0.50pp has "
             "historically marked the start of a recession."},
    {"panel": "stress", "group": "Recession risk", "id": "claims",
     "label": "Initial jobless claims (4-wk avg)", "fred": ["IC4WSA"], "freq": "weekly",
     "unit": "k", "decimals": 0, "scale": 1000, "higher_is_riskier": True,
     "note": "Four-week moving average of first-time unemployment claims — the "
             "fastest read on layoffs."},
    # -- STRESS -------------------------------------------------------------- inflation
    {"panel": "stress", "group": "Inflation risk", "id": "breakeven-5y5y",
     "label": "5y5y forward inflation breakeven", "fred": ["T5YIFR"], "freq": "daily",
     "unit": "%", "decimals": 2, "higher_is_riskier": True,
     "note": "Market-implied inflation over the five years starting five years out — "
             "the cleanest read on unanchored expectations."},
    # -- STRESS -------------------------------------------------------------- financial
    {"panel": "stress", "group": "Financial stress", "id": "vix",
     "label": "Equity volatility (VIX)", "fred": ["VIXCLS"], "freq": "daily",
     "unit": "index", "decimals": 1, "higher_is_riskier": True,
     "note": "S&P 500 30-day implied volatility — the classic fear gauge."},
    {"panel": "stress", "group": "Financial stress", "id": "credit-spread",
     "label": "Corporate credit spread (Baa − 10y)", "fred": ["BAA10Y"], "freq": "daily",
     "unit": "pp", "decimals": 2, "higher_is_riskier": True,
     "note": "Moody's Baa corporate yield over the 10-year Treasury; widens when "
             "investors demand more to hold credit risk. (FRED's ICE high-yield OAS "
             "is license-truncated to ~3 years, so this deep-history spread is used.)"},
    {"panel": "stress", "group": "Financial stress", "id": "nfci",
     "label": "Financial conditions (NFCI)", "fred": ["NFCI"], "freq": "weekly",
     "unit": "index", "decimals": 2, "higher_is_riskier": True,
     "note": "Chicago Fed National Financial Conditions Index; 0 is average, "
             "positive is tighter-than-average conditions."},
    {"panel": "stress", "group": "Financial stress", "id": "ovx",
     "label": "Oil volatility (OVX)", "fred": ["OVXCLS"], "freq": "daily",
     "unit": "index", "decimals": 1, "higher_is_riskier": True,
     "note": "CBOE crude-oil implied volatility — a proxy for energy / geopolitical "
             "stress."},

    # -- VULNERABILITIES ----------------------------------------------------- valuations
    {"panel": "vulnerabilities", "group": "Asset valuations", "id": "price-to-rent",
     "label": "Home price-to-rent", "fred": ["CSUSHPINSA", "CUUR0000SEHA"],
     "compute": "price_to_rent", "freq": "monthly",
     "unit": "index", "decimals": 1, "higher_is_riskier": True,
     "note": "Case-Shiller home prices relative to CPI rent, both indexed to the "
             "start of the sample; high means housing is dear versus rents."},
    {"panel": "vulnerabilities", "group": "Asset valuations", "id": "equity-mktcap-gdp",
     "label": "Equity market cap / GDP", "fred": ["NCBEILQ027S", "GDP"],
     "compute": "equity_mktcap_gdp", "freq": "quarterly",
     "unit": "% of GDP", "decimals": 0, "higher_is_riskier": True,
     "note": "Market value of nonfinancial corporate equities as a share of GDP "
             "(a Buffett-style gauge); high means richly valued stocks."},
    # -- VULNERABILITIES ----------------------------------------------------- borrowing
    {"panel": "vulnerabilities", "group": "Household & corporate borrowing", "id": "dsr",
     "label": "Household debt-service ratio", "fred": ["TDSP"], "freq": "quarterly",
     "unit": "%", "decimals": 2, "higher_is_riskier": True,
     "note": "Required debt payments as a share of disposable income — how stretched "
             "household cash flow is."},
    {"panel": "vulnerabilities", "group": "Household & corporate borrowing", "id": "hh-debt-gdp",
     "label": "Household debt / GDP", "fred": ["HDTGPDUSQ163N"], "freq": "quarterly",
     "unit": "% of GDP", "decimals": 1, "higher_is_riskier": True,
     "note": "Total household debt relative to the size of the economy (BIS)."},
    {"panel": "vulnerabilities", "group": "Household & corporate borrowing", "id": "corp-debt-gdp",
     "label": "Nonfinancial corporate debt / GDP", "fred": ["BCNSDODNS", "GDP"],
     "compute": "corp_debt_gdp", "freq": "quarterly",
     "unit": "% of GDP", "decimals": 0, "higher_is_riskier": True,
     "note": "Debt securities and loans of nonfinancial corporations as a share of "
             "GDP — corporate leverage across the cycle."},
    # -- VULNERABILITIES ----------------------------------------------------- funding
    {"panel": "vulnerabilities", "group": "Funding & liquidity", "id": "sofr-iorb",
     "label": "SOFR − IORB spread", "fred": ["SOFR", "IORB"],
     "compute": "sofr_iorb", "freq": "daily",
     "unit": "bps", "decimals": 0, "higher_is_riskier": True,
     "note": "Secured overnight funding rate minus interest on reserves; a positive, "
             "widening spread signals scarce cash in money markets."},
    # -- VULNERABILITIES ----------------------------------------------------- fiscal
    {"panel": "vulnerabilities", "group": "Fiscal", "id": "interest-receipts",
     "label": "Federal interest / receipts", "fred": ["A091RC1Q027SBEA", "FGRECPT"],
     "compute": "fiscal_interest_receipts", "freq": "quarterly",
     "unit": "%", "decimals": 1, "higher_is_riskier": True,
     "note": "Federal interest payments as a share of federal receipts — how much of "
             "revenue is pre-committed to servicing the debt."},
]

PANELS = [
    {"id": "stress", "title": "Stress",
     "tag": "fast · market-priced",
     "subtitle": "How scared is everyone right now"},
    {"id": "vulnerabilities", "title": "Vulnerabilities",
     "tag": "slow · balance-sheet",
     "subtitle": "How much damage a shock would do"},
]


def quadrant(stress, vuln):
    """Name the regime cell. Threshold at the 50th percentile of history."""
    hi_s, hi_v = stress >= 50, vuln >= 50
    if not hi_s and not hi_v:
        return "calm", "Calm — low stress, low vulnerability"
    if not hi_s and hi_v:
        return "building", "Building — calm markets, elevated vulnerability (the dangerous cell)"
    if hi_s and not hi_v:
        return "jitters", "Jitters — market stress without deep balance-sheet fragility"
    return "acute", "Acute — high stress and high vulnerability"


SEV_RANK = {"low": 1, "medium": 2, "high": 3}


def prob_midpoint(s):
    """Parse a probability range like '10-25%' or '<5%' to a 0-100 midpoint."""
    nums = [float(x) for x in re.findall(r"\d+(?:\.\d+)?", s or "")]
    if not nums:
        return None
    if len(nums) == 1:
        return nums[0] / (2 if "<" in s else 1)
    return sum(nums[:2]) / 2


def load_register():
    """Read register.yaml (if present) into a list of plotting-ready risk dicts."""
    p = HERE / "register.yaml"
    if not p.exists():
        return None
    try:
        import yaml  # noqa
    except ImportError:
        print("  ! PyYAML not installed — skipping register.yaml (pip install -r requirements.txt)")
        return None
    doc = yaml.safe_load(p.read_text()) or {}
    risks = []
    for r in doc.get("risks", []):
        if r.get("trend") == "retired":
            continue
        risks.append({
            "id": r["id"], "name": r["name"],
            "geography": r.get("geography", "us"), "category": r.get("category", "financial"),
            "probability": r.get("probability", ""), "prob_mid": prob_midpoint(r.get("probability", "")),
            "horizon": r.get("horizon", doc.get("meta", {}).get("horizon_default", "12m")),
            "severity": r.get("severity", "medium"), "sev_rank": SEV_RANK.get(r.get("severity", "medium"), 2),
            "trend": r.get("trend", "flat"),
            "blurb": " ".join((r.get("blurb") or "").split()),
            "triggers": r.get("triggers", []), "watching": r.get("watching", []),
            "last_changed": str(r.get("last_changed", "")),
        })
    return {"as_of": str(doc.get("meta", {}).get("as_of", "")), "risks": risks}


def load_read():
    """Read read.md into [{lead, body}] paragraphs (bold lead-in per paragraph)."""
    p = HERE / "read.md"
    if not p.exists():
        return None
    text = re.sub(r"<!--.*?-->", "", p.read_text(), flags=re.DOTALL)
    out = []
    for para in [b.strip() for b in text.split("\n\n") if b.strip()]:
        para = " ".join(para.split())
        m = re.match(r"\*\*(.+?)\*\*\s*(.*)", para)
        if m:
            out.append({"lead": m.group(1).rstrip(".").strip(), "body": m.group(2).strip()})
        else:
            out.append({"lead": "", "body": para})
    return out


def whats_changed(strips, register):
    """Auto 'what changed' bullets: biggest strip movers + register trend changes."""
    items = []
    for s in sorted((s for s in strips if s["mover"]),
                    key=lambda s: abs(s["delta_pct"]), reverse=True):
        direction = "riskier" if s["delta_pct"] > 0 else "less risky"
        items.append({"kind": "gauge", "text":
                      f"{s['label']} moved {abs(s['delta_pct']):.0f} percentile points "
                      f"{direction} (now {s['pct']:.0f}th)."})
    if register:
        arrow = {"up": "↑", "down": "↓", "new": "★"}
        for r in register["risks"]:
            if r["trend"] in arrow:
                verb = {"up": "rising", "down": "easing", "new": "newly added"}[r["trend"]]
                items.append({"kind": "register", "text":
                              f"{arrow[r['trend']]} Register: {r['name']} — {verb} "
                              f"({r['probability']} over {r['horizon']})."})
    return items


def build():
    strips = [build_strip(c) for c in INDICATORS]
    by_id = {s["id"]: s for s in strips}
    for s in strips:
        flags = []
        if s["mover"]:
            flags.append("mover")
        if s["short_history"]:
            flags.append(f"{s['window_years']:.0f}y history")
        print(f"  [{'S' if any(i['id']==s['id'] and i['panel']=='stress' for i in INDICATORS) else 'V'}] "
              f"{s['id']:<16} pct={s['pct']:>5.1f}  {s['value_fmt']:>10}  "
              f"as of {s['asof']}  {' '.join(flags)}")

    panels = []
    for p in PANELS:
        p_strips = [s for s in strips
                    if next(i for i in INDICATORS if i["id"] == s["id"])["panel"] == p["id"]]
        groups = []
        for g in dict.fromkeys(s["group"] for s in p_strips):
            groups.append({"id": g.lower().replace(" & ", "-").replace(" ", "-"),
                           "title": g,
                           "strips": [s for s in p_strips if s["group"] == g]})
        score = round(median(s["pct"] for s in p_strips), 1)
        prev = [s["prev_pct"] for s in p_strips if s["prev_pct"] is not None]
        prev_score = round(median(prev), 1) if prev else None
        panels.append({**p, "score": score, "prev_score": prev_score,
                       "n": len(p_strips), "groups": groups})

    stress_score = next(p["score"] for p in panels if p["id"] == "stress")
    vuln_score = next(p["score"] for p in panels if p["id"] == "vulnerabilities")
    stress_prev = next(p["prev_score"] for p in panels if p["id"] == "stress")
    vuln_prev = next(p["prev_score"] for p in panels if p["id"] == "vulnerabilities")
    qkey, qlabel = quadrant(stress_score, vuln_score)

    data_through = max(s["asof"] for s in strips)
    movers = sorted((s for s in strips if s["mover"]),
                    key=lambda s: abs(s["delta_pct"]), reverse=True)
    register = load_register()
    read = load_read()
    changed = whats_changed(strips, register)

    out = {
        "meta": {
            "title": "Risk Monitor",
            "data_through": data_through,
            "live_source": "FRED",
            "ramp": RAMP,
            "window_note": "Each strip's marker is the current value's percentile "
                           "over the indicator's full available history, oriented so "
                           "rightward = riskier. The ghost marker is the value ~one "
                           "month earlier (prior quarter for quarterly series). "
                           "Strips with under 10 years of history are flagged.",
            "phase_note": "Automatable core — 15 FRED gauges plus register and The "
                          "Read. Still to come: scrape/paid gauges (Shiller CAPE & "
                          "equity risk premium, FDIC bank capital, CFTC basis-trade "
                          "short) and the monthly PDF email.",
        },
        "regime": {
            "stress": stress_score, "vuln": vuln_score,
            "prev_stress": stress_prev, "prev_vuln": vuln_prev,
            "quadrant": qkey, "quadrant_label": qlabel,
        },
        "movers": [{"id": s["id"], "label": s["label"], "delta_pct": s["delta_pct"],
                    "pct": s["pct"], "panel": next(i["panel"] for i in INDICATORS
                                                   if i["id"] == s["id"])}
                   for s in movers],
        "whats_changed": changed,
        "read": read,
        "register": register,
        "panels": panels,
    }
    OUT.write_text(json.dumps(out, separators=(",", ":")) + "\n")
    print(f"\nWrote data.json — {len(strips)} strips, "
          f"stress={stress_score} vuln={vuln_score} regime={qkey!r}, "
          f"data through {data_through}.")


if __name__ == "__main__":
    build()
