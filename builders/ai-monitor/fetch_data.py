#!/usr/bin/env python3
"""
Build data.json for the AI Monitor (jasonfurman.org/test/ai-monitor/).

Phase 1 data layer. Two kinds of inputs are merged:

  * AUTO   — series pulled live at each run:
             - FRED: Indeed software-dev postings, BEA GDP contributions
               (info-processing equipment + software), nonfarm business
               productivity (output per hour).
             - Census C30 (privsatime.xlsx): data-center construction, SAAR.
             - Indeed Hiring Lab ai-tracker (GitHub, CC-BY 4.0): AI share of
               US job postings.
             - Yahoo Finance: daily adjusted closes + market caps for the AI
               equity basket, the Mag 7, and ^GSPC.
             - SSGA SPY daily holdings: Mag 7 weight in the S&P 500 (anchors
               the "S&P 493" ex-Mag-7 benchmark).
  * MANUAL — manual.json: series with no automatable source yet (Census BTOS
             AI use, hyperscaler capex, the Yale/Stanford/Challenger scoreboard
             rows, scoreboard signals, monthly bullets). See manual_SOURCES.md.

Design rules (match test/how-americans-are-doing):
  - Pure Python stdlib; runs the same on a laptop and in GitHub Actions.
  - No wall-clock timestamp in data.json — only real data dates — so a no-op
    run produces no diff.
  - Every auto block is fetched under try/except; on failure the block is
    carried forward from the existing data.json and the failure is recorded
    in meta.warnings (so one dead source never blanks the page).

Run:  python3 fetch_data.py
"""
import io
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request
import zipfile
from collections import defaultdict
from http.cookiejar import CookieJar
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent.parent  # builders/ai-monitor/ -> repo root
OUT_DIR = REPO / "lab" / "ai-monitor"  # manual.json + inhouse.json in, data.json out
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) ai-monitor/1.0"

# ---------------------------------------------------------------- definitions
# AI equity basket — fixed membership, cap-weighted (fixed shares implied by
# current caps), reviewed each January. Changes go in README changelog.
BASKET = ["NVDA", "MSFT", "GOOGL", "AMZN", "META", "AVGO", "TSM", "AMD",
          "ORCL", "PLTR", "SMCI", "VRT", "ANET", "MU", "DELL"]
# Mag 7 for the ex-Mag-7 benchmark and the concentration stat. SPY holds both
# Alphabet share classes, so GOOG rides along for the weight sum.
MAG7 = ["AAPL", "MSFT", "GOOGL", "GOOG", "AMZN", "NVDA", "META", "TSLA"]

FRED_SOFTDEV = "IHLIDXUSTPSOFTDEVE"       # Indeed US software-dev postings, daily index
FRED_CONTRIB_IPE = "Y034RY2Q224SBEA"      # GDP contribution: info-processing equipment
FRED_CONTRIB_SW = "B985RY2Q224SBEA"       # GDP contribution: software
FRED_OPHNFB = "OPHNFB"                    # Nonfarm business real output per hour

C30_URL = "https://www.census.gov/construction/c30/xlsx/privsatime.xlsx"
INDEED_AI_URL = "https://raw.githubusercontent.com/hiring-lab/ai-tracker/main/AI_posting.csv"
SPY_URL = ("https://www.ssga.com/us/en/intermediary/library-content/products/"
           "fund-data/etfs/us/holdings-daily-us-en-spy.xlsx")


# ---------------------------------------------------------------------- creds
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


# ---------------------------------------------------------------------- fetch
def http_get(url, headers=None, timeout=45, opener=None, retries=3):
    req = urllib.request.Request(url, headers={"User-Agent": UA, **(headers or {})})
    for attempt in range(retries + 1):
        try:
            fn = opener.open if opener else urllib.request.urlopen
            with fn(req, timeout=timeout) as r:
                return r.read()
        except Exception:
            if attempt == retries:
                raise
            time.sleep(1.5 * (attempt + 1))


def fred_obs(code, start="2010-01-01"):
    """[(y, m, value)] for a FRED series."""
    q = urllib.parse.urlencode({"series_id": code, "api_key": KEY,
                                "file_type": "json", "observation_start": start})
    obs = json.loads(http_get(f"https://api.stlouisfed.org/fred/series/observations?{q}"))["observations"]
    out = []
    for o in obs:
        if o["value"] in (".", "", None):
            continue
        out.append((int(o["date"][:4]), int(o["date"][5:7]), float(o["value"])))
    if not out:
        raise RuntimeError(f"{code}: no observations")
    return out


# ----------------------------------------------------------------- xlsx utils
def xlsx_rows(blob):
    """Minimal XLSX reader: list of rows, each {col_letter: value}, for sheet1."""
    z = zipfile.ZipFile(io.BytesIO(blob))
    shared = []
    if "xl/sharedStrings.xml" in z.namelist():
        ss = z.read("xl/sharedStrings.xml").decode("utf-8", "replace")
        # each <si> may contain multiple <t> runs; join them
        shared = ["".join(re.findall(r"<t[^>]*>([^<]*)</t>", si))
                  for si in re.findall(r"<si>(.*?)</si>", ss, re.S)]
    sheet = z.read("xl/worksheets/sheet1.xml").decode("utf-8", "replace")
    rows = []
    for rxml in re.findall(r"<row[^>]*>(.*?)</row>", sheet, re.S):
        row = {}
        for cxml in re.findall(r"<c [^>]*?/>|<c [^>]*?>.*?</c>", rxml, re.S):
            ref = re.search(r'r="([A-Z]+)(\d+)"', cxml)
            if not ref:
                continue
            col = ref.group(1)
            v = re.search(r"<v>([^<]*)</v>", cxml)
            if v is None:
                t = re.search(r"<t[^>]*>([^<]*)</t>", cxml)  # inline string
                if t:
                    row[col] = t.group(1).strip()
                continue
            if 't="s"' in cxml:
                row[col] = shared[int(v.group(1))].strip()
            else:
                try:
                    row[col] = float(v.group(1))
                except ValueError:
                    row[col] = v.group(1).strip()
        rows.append(row)
    return rows


def excel_serial_to_ym(n):
    """Excel serial date -> (year, month). Good enough for month-start dates."""
    days = int(n) - 25569  # to unix epoch days
    t = time.gmtime(days * 86400)
    return t.tm_year, t.tm_mon


MONTHS = {m: i + 1 for i, m in enumerate(
    ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"])}


def parse_month_label(v):
    """'Jan-25' / 'Jan 2025' / excel serial -> (year, month) or None."""
    if isinstance(v, float):
        return excel_serial_to_ym(v)
    s = str(v).strip()
    m = re.match(r"([A-Za-z]{3})[a-z]*[- ]'?(\d{2,4})", s)
    if m and m.group(1)[:3].title() in MONTHS:
        yr = int(m.group(2))
        return (yr if yr > 100 else 2000 + yr), MONTHS[m.group(1)[:3].title()]
    return None


# ------------------------------------------------------------------ time axes
def ym_x(y, m):
    return round(y + (m - 0.5) / 12, 4)


def yq_x(y, q):
    return round(y + (q * 3 - 1.5) / 12, 4)


def monthly_mean(obs):
    """[(y,m,v)] daily/weekly -> [[x, mean]] monthly."""
    by = defaultdict(list)
    for y, m, v in obs:
        by[(y, m)].append(v)
    return [[ym_x(y, m), round(sum(vs) / len(vs), 2)] for (y, m), vs in sorted(by.items())]


def label_ym(y, m):
    return f"{list(MONTHS)[m - 1]} {y}"


# --------------------------------------------------------------- yahoo finance
def yahoo_opener():
    jar = CookieJar()
    op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
    try:
        http_get("https://fc.yahoo.com", opener=op, retries=0)
    except urllib.error.HTTPError:
        pass  # 404 is expected — the request exists only to set the cookie
    crumb = http_get("https://query1.finance.yahoo.com/v1/test/getcrumb",
                     opener=op).decode().strip()
    if not crumb or "<" in crumb:
        raise RuntimeError("Yahoo crumb unavailable")
    return op, crumb


def yahoo_history(sym, opener, rng="4y"):
    """{date_str: adjclose} daily."""
    url = (f"https://query1.finance.yahoo.com/v8/finance/chart/{urllib.parse.quote(sym)}"
           f"?range={rng}&interval=1d")
    r = json.loads(http_get(url, opener=opener))["chart"]["result"][0]
    ts = r.get("timestamp") or []
    adj = r["indicators"].get("adjclose", [{}])[0].get("adjclose") or \
        r["indicators"]["quote"][0]["close"]
    out = {}
    for t, v in zip(ts, adj):
        if v is None:
            continue
        g = time.gmtime(t)
        out[f"{g.tm_year:04d}-{g.tm_mon:02d}-{g.tm_mday:02d}"] = v
    if not out:
        raise RuntimeError(f"{sym}: no price history")
    return out


def yahoo_caps(symbols, opener, crumb):
    """{sym: market_cap} via quoteSummary."""
    caps = {}
    for s in symbols:
        url = (f"https://query1.finance.yahoo.com/v10/finance/quoteSummary/"
               f"{urllib.parse.quote(s)}?modules=price&crumb={urllib.parse.quote(crumb)}")
        p = json.loads(http_get(url, opener=opener))["quoteSummary"]["result"][0]["price"]
        caps[s] = float(p["marketCap"]["raw"])
    return caps


# ------------------------------------------------------------- block builders
def build_indeed_ai():
    txt = http_get(INDEED_AI_URL).decode()
    pts = []
    for line in txt.splitlines()[1:]:
        parts = line.split(",")
        if len(parts) >= 3 and parts[1] == "US":
            y, m = int(parts[0][:4]), int(parts[0][5:7])
            pts.append((y, m, round(float(parts[2]), 2)))
    if not pts:
        raise RuntimeError("Indeed ai-tracker: no US rows")
    pts.sort()
    (ly, lm, lv), (py, pm, pv) = pts[-1], pts[-2]
    return {
        "series": [[ym_x(y, m), v] for y, m, v in pts],
        "latest": lv, "prev": pv, "asof": label_ym(ly, lm),
    }


def build_softdev():
    obs = fred_obs(FRED_SOFTDEV, start="2018-01-01")
    monthly = monthly_mean(obs)
    ly, lm, _ = obs[-1]
    return {
        "series": monthly,
        "latest": monthly[-1][1], "prev": monthly[-2][1],
        "asof": label_ym(ly, lm),
    }


def build_datacenter():
    rows = xlsx_rows(http_get(C30_URL))
    # locate header row containing "Data center" and the date column
    target_col, date_col, start_i = None, None, None
    for i, row in enumerate(rows):
        for col, v in row.items():
            if isinstance(v, str) and v.strip() == "Data center":
                target_col, start_i = col, i + 1
            if isinstance(v, str) and v.strip() == "Date":
                date_col = col
        if target_col:
            break
    if not target_col:
        raise RuntimeError("C30: 'Data center' column not found")
    pts = []
    for row in rows[start_i:]:
        dt = parse_month_label(row.get(date_col, row.get("A")))
        v = row.get(target_col)
        if dt and isinstance(v, float):
            pts.append((dt[0], dt[1], v))
    pts.sort()
    if len(pts) < 14:
        raise RuntimeError(f"C30: only {len(pts)} data-center points parsed")
    ly, lm, lv = pts[-1]
    yoy = None
    prior = [p for p in pts if (p[0], p[1]) == (ly - 1, lm)]
    if prior:
        yoy = round((lv / prior[0][2] - 1) * 100, 1)
    return {
        "series": [[ym_x(y, m), round(v / 1000, 2)] for y, m, v in pts],  # $bn SAAR
        "latest_bn": round(lv / 1000, 1), "yoy_pct": yoy, "asof": label_ym(ly, lm),
    }


def build_gdp_contrib():
    ipe = fred_obs(FRED_CONTRIB_IPE, start="2015-01-01")
    sw = fred_obs(FRED_CONTRIB_SW, start="2015-01-01")
    swd = {(y, m): v for y, m, v in sw}
    pts = [(y, m, round(v + swd[(y, m)], 2)) for y, m, v in ipe if (y, m) in swd]
    ly, lm, lv = pts[-1]
    q = (lm - 1) // 3 + 1
    return {
        "series": [[yq_x(y, (m - 1) // 3 + 1), v] for y, m, v in pts],
        "latest_pp": lv, "asof": f"{ly}Q{q}",
    }


def build_productivity():
    obs = fred_obs(FRED_OPHNFB, start="2005-01-01")
    yoy = []
    for i in range(4, len(obs)):
        y, m, v = obs[i]
        yoy.append((y, m, round((v / obs[i - 4][2] - 1) * 100, 2)))
    base = [v for y, m, v in yoy if 2015 <= y <= 2019]
    ref = round(sum(base) / len(base), 2)
    # level vs a 2015-19 log-linear trend, indexed 2019Q4 = 100
    lvl = [(y, m, v) for y, m, v in obs if y >= 2015]
    import math
    fit = [(i, math.log(v)) for i, (y, m, v) in enumerate(lvl) if y <= 2019]
    n = len(fit)
    sx = sum(i for i, _ in fit); sy = sum(l for _, l in fit)
    sxx = sum(i * i for i, _ in fit); sxy = sum(i * l for i, l in fit)
    b = (n * sxy - sx * sy) / (n * sxx - sx * sx)
    a = (sy - b * sx) / n
    anchor = next(v for y, m, v in lvl if (y, m) == (2019, 10))
    ly, lm, lv = yoy[-1]
    return {
        "yoy_series": [[yq_x(y, (m - 1) // 3 + 1), v] for y, m, v in yoy],
        "ref_2015_19": ref,
        "level_series": [[yq_x(y, (m - 1) // 3 + 1), round(v / anchor * 100, 2)]
                         for y, m, v in lvl],
        "trend_series": [[yq_x(y, (m - 1) // 3 + 1),
                          round(math.exp(a + b * i) / anchor * 100, 2)]
                         for i, (y, m, v) in enumerate(lvl)],
        "latest_yoy": lv, "asof": f"{ly}Q{(lm - 1) // 3 + 1}",
    }


def spy_mag7_weight():
    rows = xlsx_rows(http_get(SPY_URL))
    tick_col, wt_col, start_i, asof = None, None, None, None
    for i, row in enumerate(rows):
        for col, v in row.items():
            if isinstance(v, str):
                if v.strip().lower() == "ticker":
                    tick_col, start_i = col, i + 1
                if v.strip().lower() in ("weight", "weight (%)"):
                    wt_col = col
                m = re.search(r"As of (\d{2})-([A-Za-z]{3})-(\d{4})", v)
                if m:
                    asof = f"{m.group(2)} {m.group(3)}"
        if tick_col and wt_col:
            break
    if not (tick_col and wt_col):
        raise RuntimeError("SPY holdings: header row not found")
    w7 = 0.0
    for row in rows[start_i:]:
        t = row.get(tick_col)
        w = row.get(wt_col)
        if isinstance(t, str) and t.strip() in MAG7 and isinstance(w, float):
            w7 += w
    if not 15 <= w7 <= 60:
        raise RuntimeError(f"SPY holdings: implausible Mag 7 weight {w7}")
    return w7 / 100.0, asof


def build_markets():
    op, crumb = yahoo_opener()
    need = sorted(set(BASKET + MAG7))
    hist = {s: yahoo_history(s, op) for s in need}
    spx = yahoo_history("^GSPC", op)
    caps = yahoo_caps(BASKET, op, crumb)
    w7_0, spy_asof = spy_mag7_weight()

    # common daily dates (all basket + mag7 + spx)
    dates = sorted(set.intersection(*(set(h) for h in hist.values()), set(spx)))
    if len(dates) < 250:
        raise RuntimeError(f"markets: only {len(dates)} common dates")

    # AI basket: cap-weighted with fixed shares implied by latest caps
    shares = {s: caps[s] / hist[s][dates[-1]] for s in BASKET}
    bcap = [sum(shares[s] * hist[s][d] for s in BASKET) for d in dates]

    # Mag 7 weight through time: scale today's SPY weight by relative performance
    m7_0 = sum(hist[s][dates[-1]] for s in MAG7)
    w7 = []
    for i, d in enumerate(dates):
        m7rel = sum(hist[s][d] for s in MAG7) / m7_0
        spxrel = spx[d] / spx[dates[-1]]
        w7.append(min(w7_0 * m7rel / spxrel, 0.9))

    # S&P 493 (ex-Mag 7): back out from index return and Mag 7 return
    i493 = [100.0]
    for i in range(1, len(dates)):
        r_spx = spx[dates[i]] / spx[dates[i - 1]] - 1
        m7_r = (sum(hist[s][dates[i]] for s in MAG7) /
                sum(hist[s][dates[i - 1]] for s in MAG7) - 1)
        r493 = (r_spx - w7[i - 1] * m7_r) / (1 - w7[i - 1])
        i493.append(i493[-1] * (1 + r493))

    def one_month_ret(levels):
        # ~21 trading days
        return round((levels[-1] / levels[-22] - 1) * 100, 1)

    ret_basket = one_month_ret(bcap)
    ret_493 = one_month_ret(i493)

    # monthly (last trading day) series for charts, indexed 100 at start
    def monthly_last(vals):
        by = {}
        for d, v in zip(dates, vals):
            by[d[:7]] = (d, v)
        pts = sorted(by.values())
        base = pts[0][1]
        return [[ym_x(int(d[:4]), int(d[5:7])), round(v / base * 100, 2)] for d, v in pts]

    ld = dates[-1]
    asof = f"{list(MONTHS)[int(ld[5:7]) - 1]} {int(ld[8:10])}, {ld[:4]}"
    basket_wts = sorted(((s, caps[s]) for s in BASKET), key=lambda x: -x[1])
    total_cap = sum(caps.values())
    return {
        "basket_series": monthly_last(bcap),
        "sp493_series": monthly_last(i493),
        "mag7_share_series": [[ym_x(int(d[:4]), int(d[5:7])), round(w * 100, 1)]
                              for (d, w) in sorted({dd[:7]: (dd, ww) for dd, ww in
                                                    zip(dates, w7)}.values())],
        "ret_1m_basket": ret_basket, "ret_1m_sp493": ret_493,
        "spread_1m": round(ret_basket - ret_493, 1),
        "mag7_share_now": round(w7[-1] * 100, 1),
        "asof": asof, "spy_weights_asof": spy_asof,
        "basket_weights": [{"t": s, "w": round(c / total_cap * 100, 1)}
                           for s, c in basket_wts],
    }


# -------------------------------------------------------------- scoreboard def
SCOREBOARD_DEF = [
    {"id": "ybl_dissimilarity", "n": 1, "mode": "manual",
     "indicator": "Occupational mix shift (dissimilarity vs. pre-AI baseline)",
     "source": "Yale Budget Lab", "url": "https://budgetlab.yale.edu"},
    {"id": "ybl_quintiles", "n": 2, "mode": "inhouse",
     "indicator": "Employment shares by AI-exposure quintile",
     "source": "In-house (CPS microdata)", "url": None},
    {"id": "canaries_early_career", "n": 3, "mode": "manual",
     "indicator": "Early-career (22–25) employment, top exposure quintile",
     "source": "Stanford DEL–ADP Canaries", "url": "https://indicators.stanford.edu"},
    {"id": "canaries_auto_aug", "n": 4, "mode": "manual",
     "indicator": "Automation-vs-augmentation employment split",
     "source": "Stanford DEL–ADP Canaries", "url": "https://indicators.stanford.edu"},
    {"id": "indeed_ai_share", "n": 5, "mode": "auto",
     "indicator": "AI share of US job postings",
     "source": "Indeed Hiring Lab (CC-BY 4.0)",
     "url": "https://github.com/hiring-lab/ai-tracker"},
    {"id": "indeed_swdev", "n": 6, "mode": "auto",
     "indicator": "Software-development postings index",
     "source": "Indeed via FRED",
     "url": "https://fred.stlouisfed.org/series/IHLIDXUSTPSOFTDEVE"},
    {"id": "challenger_ai_share", "n": 7, "mode": "manual",
     "indicator": "AI-cited layoff announcements, share of total cuts",
     "source": "Challenger, Gray & Christmas", "url": "https://www.challengergray.com"},
    {"id": "recent_grad_gap", "n": 8, "mode": "inhouse",
     "indicator": "Recent-grad vs. overall unemployment gap",
     "source": "In-house (CPS microdata)", "url": None},
]


def build_scoreboard(manual, indeed_ai, softdev, inhouse):
    rows = []
    signals = []
    msb = manual.get("scoreboard", {})
    msig = manual.get("signals", {})
    isb = (inhouse or {}).get("scoreboard", {})
    for d in SCOREBOARD_DEF:
        row = {k: d[k] for k in ("id", "n", "indicator", "source", "url", "mode")}
        if d["mode"] == "inhouse" and isb.get(d["id"], {}).get("latest") is not None:
            m = isb[d["id"]]
            row.update(latest=m.get("latest"), asof=m.get("asof"),
                       delta=m.get("delta"), delta_units=m.get("delta_units"),
                       note=m.get("note"))
        elif d["mode"] == "auto" and d["id"] == "indeed_ai_share" and indeed_ai:
            row.update(latest=f"{indeed_ai['latest']}%", asof=indeed_ai["asof"],
                       delta=round(indeed_ai["latest"] - indeed_ai["prev"], 2),
                       delta_units="pp vs prior month",
                       chart={"title": "AI share of US job postings",
                              "units": "% of postings", "freq": "m",
                              "asof": indeed_ai["asof"],
                              "source": "Indeed Hiring Lab AI tracker (CC-BY 4.0)",
                              "series": [{"name": "AI share", "data": indeed_ai["series"]}]})
        elif d["mode"] == "auto" and d["id"] == "indeed_swdev" and softdev:
            row.update(latest=f"{softdev['latest']}", asof=softdev["asof"],
                       delta=round(softdev["latest"] - softdev["prev"], 1),
                       delta_units="pts vs prior month",
                       chart={"title": "US software-development job postings",
                              "units": "index, Feb 1 2020 = 100", "freq": "m",
                              "asof": softdev["asof"],
                              "source": "Indeed via FRED (IHLIDXUSTPSOFTDEVE)",
                              "series": [{"name": "Postings index",
                                          "data": softdev["series"]}]})
        else:
            m = msb.get(d["id"], {})
            row.update(latest=m.get("latest"), asof=m.get("asof"),
                       delta=m.get("delta"), delta_units=m.get("delta_units"),
                       note=m.get("note"))
        sig = msig.get(d["id"])
        row["signal"] = sig
        if isinstance(sig, (int, float)):
            signals.append(sig)
        rows.append(row)
    composite = round(sum(signals) / len(signals), 1) if len(signals) >= 6 else None
    return {"rows": rows, "composite": composite,
            "scored": len(signals), "of": len(rows),
            "reconciliation": manual.get("reconciliation")}


# ------------------------------------------------------------------- assemble
def main():
    manual = json.loads((OUT_DIR / "manual.json").read_text())
    inhouse = None
    if (OUT_DIR / "inhouse.json").exists():
        inhouse = json.loads((OUT_DIR / "inhouse.json").read_text())
    old = {}
    if (OUT_DIR / "data.json").exists():
        old = json.loads((OUT_DIR / "data.json").read_text())
    warnings = []

    def block(name, fn):
        try:
            return fn()
        except Exception as e:  # noqa
            warnings.append(f"{name}: {type(e).__name__}: {e}")
            return (old.get("auto") or {}).get(name)

    auto = {
        "indeed_ai": block("indeed_ai", build_indeed_ai),
        "softdev": block("softdev", build_softdev),
        "datacenter": block("datacenter", build_datacenter),
        "gdp_contrib": block("gdp_contrib", build_gdp_contrib),
        "productivity": block("productivity", build_productivity),
        "markets": block("markets", build_markets),
    }

    scoreboard = build_scoreboard(manual, auto["indeed_ai"], auto["softdev"], inhouse)

    btos = manual.get("btos", {})
    mk, pr, dc = auto["markets"], auto["productivity"], auto["datacenter"]

    tiles = [
        {"id": "adoption", "label": "Adoption",
         "stat": (f"{btos['share_pct']}%" if btos.get("share_pct") is not None else None),
         "sub": "of firms use AI (Census BTOS)",
         "delta": btos.get("delta_3m_pp"), "delta_label": "pp vs 3 months prior",
         "asof": btos.get("asof"), "spark": (btos.get("series") or [])[-24:]},
        {"id": "buildout", "label": "Buildout",
         "stat": (f"${dc['latest_bn']}bn" if dc else None),
         "sub": "data-center construction, SAAR",
         "delta": (dc or {}).get("yoy_pct"), "delta_label": "% y/y",
         "asof": (dc or {}).get("asof"),
         "spark": (dc["series"][-24:] if dc else [])},
        {"id": "markets", "label": "Markets",
         "stat": (f"{mk['ret_1m_basket']:+}%" if mk else None),
         "sub": "AI basket, 1-month return",
         "delta": (mk or {}).get("spread_1m"), "delta_label": "pp vs S&P 493",
         "asof": (mk or {}).get("asof"),
         "spark": (mk["basket_series"][-24:] if mk else [])},
        {"id": "productivity", "label": "Productivity",
         "stat": (f"{pr['latest_yoy']}%" if pr else None),
         "sub": "output per hour, 4-quarter change",
         "delta": (round(pr["latest_yoy"] - pr["ref_2015_19"], 1) if pr else None),
         "delta_label": "pp vs 2015–19 average", "asof": (pr or {}).get("asof"),
         "spark": (pr["yoy_series"][-16:] if pr else [])},
        {"id": "employment", "label": "Employment",
         "stat": (str(scoreboard["composite"]) if scoreboard["composite"] is not None else None),
         "sub": f"disruption score, 0–2 scale ({scoreboard['scored']}/{scoreboard['of']} scored)",
         "delta": None, "delta_label": None, "asof": None, "spark": []},
    ]

    capex = manual.get("capex", {}).get("quarters") or []
    macro = {
        "adoption": {
            "title": "Firms using AI (Census BTOS)", "units": "% of firms", "freq": "m",
            "asof": btos.get("asof"), "source": "Census Business Trends and Outlook Survey — hand-entered; automation planned",
            "series": ([{"name": "All firms", "data": btos.get("series") or []}]),
            "empty_msg": "Awaiting first manual BTOS entry — see manual.json.",
        },
        "buildout": {
            "title": "Data-center construction spending", "units": "$bn, seasonally adjusted annual rate", "freq": "m",
            "asof": (dc or {}).get("asof"), "source": "Census Value of Construction Put in Place (C30)",
            "series": ([{"name": "Data centers", "data": dc["series"]}] if dc else []),
            "stat": (f"{auto['gdp_contrib']['latest_pp']:+.2f}pp" if auto["gdp_contrib"] else None),
            "stat_label": (f"contribution of info-processing equipment + software investment to GDP growth, {auto['gdp_contrib']['asof']}" if auto["gdp_contrib"] else None),
            "capex_quarters": capex,
        },
        "markets": {
            "title": "AI basket vs. the rest of the market", "units": "total return index (start = 100)", "freq": "m",
            "asof": (mk or {}).get("asof"),
            "source": "Yahoo Finance daily adjusted closes; S&P 493 = S&P 500 ex–Mag 7, anchored to SPY holdings weights (SSGA)",
            "series": ([{"name": "AI basket (15 stocks)", "data": mk["basket_series"]},
                        {"name": "S&P 493", "data": mk["sp493_series"]}] if mk else []),
            "stat": (f"{mk['mag7_share_now']}%" if mk else None),
            "stat_label": "Mag 7 share of S&P 500 market cap",
            "mag7_series": (mk or {}).get("mag7_share_series"),
            "basket_weights": (mk or {}).get("basket_weights"),
        },
        "productivity": {
            "title": "Labor productivity vs. pre-AI trend", "units": "index, 2019Q4 = 100", "freq": "q",
            "asof": (pr or {}).get("asof"), "source": "BLS nonfarm business output per hour (via FRED); trend fit 2015–19",
            "series": ([{"name": "Actual", "data": pr["level_series"]},
                        {"name": "2015–19 trend", "data": pr["trend_series"]}] if pr else []),
            "stat": (f"{pr['latest_yoy']}%" if pr else None),
            "stat_label": (f"4-quarter growth vs {pr['ref_2015_19']}% average pace in 2015–19" if pr else None),
        },
    }

    gdpc = auto["gdp_contrib"]
    out = {
        "meta": {
            "title": "AI Monitor",
            "issue": manual.get("issue"),
            "warnings": warnings,
            "basket": BASKET,
        },
        "bullets": manual.get("bullets") or [],
        "bottom_line": manual.get("bottom_line"),
        "tiles": tiles,
        "scoreboard": scoreboard,
        "inhouse": ({"meta": inhouse["meta"], "charts": inhouse["charts"]}
                    if inhouse else None),
        "macro": macro,
        "gdp_contrib_series": (
            {"title": "AI-related investment contribution to GDP growth",
             "units": "percentage points of annualized quarterly growth", "freq": "q",
             "asof": gdpc["asof"],
             "source": "BEA NIPA via FRED: info-processing equipment + software contributions",
             "series": [{"name": "Contribution", "data": gdpc["series"]}]}
            if gdpc else None),
    }

    (OUT_DIR / "data.json").write_text(json.dumps(out, ensure_ascii=True) + "\n")
    n_series = sum(1 for b in auto.values() if b)
    print(f"wrote data.json ({n_series}/6 auto blocks fresh)")
    for w in warnings:
        print("  WARNING:", w, file=sys.stderr)
    if warnings and n_series == 0:
        sys.exit(1)


KEY = fred_key()

if __name__ == "__main__":
    main()
