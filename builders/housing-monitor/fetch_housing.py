#!/usr/bin/env python3
"""
fetch_housing.py — daily refresh for the Housing Bubble Monitor lab page.

Reads housing_config.yaml (the editorial layout + per-indicator `source:` specs),
pulls live values, and writes data.json in the shape housing.js renders.

DATA SOURCING — FRED, directly. Every config key that has a `source:` block is a FRED
series id; there is one fetch path and no vendor in front of it.

RESILIENCE: every indicator is computed in its own try/except. On any failure the
indicator keeps the `val`/`spark`/`v` already written in config, so the page never
blanks — exactly like the dashboard's maclow job. A per-run source tally
(fred / config) is written to data.json for transparency.

Credentials resolve env var → ~/.config/macro-dashboard/.env (same as the chartbook
clients): FRED_API_KEY.

Run:
  python fetch_housing.py            # writes ./data.json
  HOUSING_OUT=/path/data.json python fetch_housing.py   # cloud Action override
"""
from __future__ import annotations

import datetime as dt
import json
import os
import sys
import time
from pathlib import Path

import requests
import yaml

HERE = Path(__file__).resolve().parent
REPO = HERE.parent.parent  # builders/housing-monitor/ -> repo root
CONFIG_PATH = HERE / "housing_config.yaml"
OUT_PATH = (Path(os.environ["HOUSING_OUT"]) if os.environ.get("HOUSING_OUT")
            else REPO / "lab" / "housing-monitor" / "data.json")
ENV_FILE = Path.home() / ".config" / "macro-dashboard" / ".env"

SESSION = requests.Session()      # used by the FRED REST client below
_MINUS = "−"  # typographic minus, matches the look prototype


# ── credentials ──────────────────────────────────────────────────────────────
def _env_file(key):
    if ENV_FILE.exists():
        for line in ENV_FILE.read_text().splitlines():
            line = line.strip()
            if line.startswith(key + "=") and not line.startswith("#"):
                return line.split("=", 1)[1].strip() or None
    return None


def _cred(key):
    return os.environ.get(key) or _env_file(key)


# ── FRED REST ──────────────────────────────────────────────────────────────--
_fred_cache = {}


def fred_series(series_id, start="1995-01-01"):
    if series_id in _fred_cache:
        return _fred_cache[series_id]
    key = _cred("FRED_API_KEY")
    if not key:
        raise RuntimeError("no FRED_API_KEY")
    r = SESSION.get("https://api.stlouisfed.org/fred/series/observations", params={
        "series_id": series_id, "api_key": key, "file_type": "json",
        "observation_start": start}, timeout=60)
    r.raise_for_status()
    obs = []
    for o in r.json().get("observations", []):
        if o["value"] not in (".", ""):
            obs.append((dt.date.fromisoformat(o["date"]), float(o["value"])))
    obs.sort(key=lambda o: o[0])
    if not obs:
        raise RuntimeError("FRED %s: no observations" % series_id)
    _fred_cache[series_id] = obs
    return obs


# ── resolver ─────────────────────────────────────────────────────────────────
USED = {"fred": 0, "config": 0}


def get_series(key):
    """Series for a config key — every key is a FRED series id. Returns sorted
    list[(date, value)]. Raises if FRED yields nothing, which the caller turns into
    "keep the editorial value already in config"."""
    obs = fred_series(key)
    USED["fred"] += 1
    return obs


# ── helpers ──────────────────────────────────────────────────────────────────
def latest(obs):
    return obs[-1]


def val_on_or_before(obs, target):
    best = None
    for d, v in obs:
        if d <= target:
            best = v
        else:
            break
    return best


def year_ago_value(obs):
    ld, _ = obs[-1]
    return val_on_or_before(obs, ld - dt.timedelta(days=365))


def mean_in_year(obs, year):
    vals = [v for d, v in obs if d.year == year]
    return sum(vals) / len(vals) if vals else None


def fmt_num(x, spec):
    spec = spec or {}
    if spec.get("scale"):
        x = x * float(spec["scale"])
    decimals = int(spec.get("decimals", 1))
    body = ("{:,.%df}" % decimals).format(abs(x)) if spec.get("thousands") \
        else ("{:.%df}" % decimals).format(abs(x))
    sign = ""
    if x < 0:
        sign = _MINUS
    elif spec.get("sign"):
        sign = "+"
    return spec.get("prefix", "") + sign + body + spec.get("suffix", "")


# ── transforms ────────────────────────────────────────────────────────────---
def t_yoy(src):
    obs = get_series(src["series"])
    ya = year_ago_value(obs)
    pct = (obs[-1][1] / ya - 1) * 100
    return {"val": fmt_num(pct, src.get("fmt")), "date": obs[-1][0]}


def t_real_yoy(src):
    cs = get_series(src["series"])
    cpi = get_series(src["deflator"])
    real = [(d, v / val_on_or_before(cpi, d)) for d, v in cs if val_on_or_before(cpi, d)]
    ld, lv = real[-1]
    ya = val_on_or_before(real, ld - dt.timedelta(days=365))
    pct = (lv / ya - 1) * 100
    return {"val": fmt_num(pct, src.get("fmt")), "date": ld}


def t_ratio_index(src):
    num = dict(get_series(src["num"]))
    den = get_series(src["den"])
    den_sorted = sorted(den)
    by = int(src["base_year"])

    def den_at(d):
        best = None
        for dd, vv in den_sorted:
            if dd <= d:
                best = vv
            else:
                break
        return best
    ratios = {}
    for d, v in sorted(num.items()):
        dv = den_at(d)
        if dv:
            ratios[d] = v / dv
    base_vals = [r for d, r in ratios.items() if d.year == by]
    base = sum(base_vals) / len(base_vals)
    ld = max(ratios)
    idx = ratios[ld] / base * 100
    return {"val": fmt_num(idx, src.get("fmt")), "date": ld}


def t_ratio(src):
    num = get_series(src["num"])
    den = get_series(src["den"])
    r = num[-1][1] / den[-1][1]
    return {"val": fmt_num(r, src.get("fmt")), "date": num[-1][0]}


def t_pct_vs_year(src):
    obs = get_series(src["series"])
    base = mean_in_year(obs, int(src["base_year"]))
    pct = (obs[-1][1] / base - 1) * 100
    return {"val": fmt_num(pct, src.get("fmt")), "date": obs[-1][0]}


def t_weekly_spark(src):
    obs = get_series(src["series"])
    n = int(src.get("n", 14))
    spark = [round(v, 4) for _, v in obs[-n:]]
    return {"val": fmt_num(obs[-1][1], src.get("fmt")), "spark": spark, "date": obs[-1][0]}


def _payment(principal, annual_rate_pct, term_months):
    r = annual_rate_pct / 100.0 / 12.0
    if r == 0:
        return principal / term_months
    return principal * r / (1 - (1 + r) ** (-term_months))


def t_implied_pi(src):
    rate = get_series(src["rate"])
    price = get_series(src["price_series"])[-1][1]
    principal = price * (1 - float(src.get("down", 0.20)))
    term = int(src.get("term_months", 360))
    n = int(src.get("n", 14))
    pi_now = _payment(principal, rate[-1][1], term)
    spark = [round(_payment(principal, rv, term)) for _, rv in rate[-n:]]
    return {"val": fmt_num(pi_now, src.get("fmt")), "spark": spark, "date": rate[-1][0]}


def t_level(src):
    obs = get_series(src["series"])
    return {"val": fmt_num(obs[-1][1], src.get("fmt")), "date": obs[-1][0]}


def t_table_level(src):
    obs = get_series(src["series"])
    fmt, dfmt = src.get("fmt"), src.get("dfmt")
    latest_v = obs[-1][1]
    prior_v = obs[-2][1] if len(obs) > 1 else latest_v
    ya = year_ago_value(obs)
    out = {"v": fmt_num(latest_v, fmt), "p": fmt_num(prior_v, fmt), "date": obs[-1][0]}
    mode = src.get("delta")
    if mode == "yoy_pct" and ya:
        delta = (latest_v / ya - 1) * 100
        out["d"] = fmt_num(delta, dfmt)
        out["dir"] = "up" if delta > 0 else ("dn" if delta < 0 else "flat")
    elif mode == "yoy_diff" and ya is not None:
        delta = latest_v - ya
        out["d"] = fmt_num(delta, dfmt)
        out["dir"] = "up" if delta > 0 else ("dn" if delta < 0 else "flat")
    return out


def _ratio_series(num_obs, den_obs):
    """Aligned ratio series for two observation lists, keyed on the numerator's
    dates (denominator carried-forward to that date). Returns sorted [(d, r)]."""
    out = []
    for d, v in num_obs:
        dv = val_on_or_before(den_obs, d)
        if dv:
            out.append((d, v / dv))
    return out


def t_table_ratio(src):
    """A table row whose value is the ratio of two series (e.g. price-reduced
    share = price-reduced count / active-listing count), with a YoY delta on the
    ratio itself. `delta: yoy_diff` reports the change in the (scaled) ratio."""
    num = get_series(src["num"])
    den = get_series(src["den"])
    rat = _ratio_series(num, den)
    fmt, dfmt = src.get("fmt"), src.get("dfmt")
    ld, lv = rat[-1]
    prior_v = rat[-2][1] if len(rat) > 1 else lv
    ya = val_on_or_before(rat, ld - dt.timedelta(days=365))
    out = {"v": fmt_num(lv, fmt), "p": fmt_num(prior_v, fmt), "date": ld}
    if src.get("delta") == "yoy_diff" and ya is not None:
        sc = float((dfmt or {}).get("scale", (fmt or {}).get("scale", 1)))
        delta = (lv - ya) * sc
        # dfmt scale already applied; emit on a unit-scaled spec
        d_spec = dict(dfmt or {})
        d_spec.pop("scale", None)
        out["d"] = fmt_num(delta, d_spec)
        out["dir"] = "up" if delta > 0 else ("dn" if delta < 0 else "flat")
    return out


TRANSFORMS = {
    "yoy": t_yoy, "real_yoy": t_real_yoy, "ratio_index": t_ratio_index,
    "ratio": t_ratio, "pct_vs_year": t_pct_vs_year, "weekly_spark": t_weekly_spark,
    "implied_pi": t_implied_pi, "level": t_level, "table_level": t_table_level,
    "table_ratio": t_table_ratio,
}

MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]


def fmt_date(d):
    return "%s %d %d" % (MONTHS[d.month - 1], d.day, d.year) if d else ""


# ── main ───────────────────────────────────────────────────────────────────--
def main():
    cfg = yaml.safe_load(CONFIG_PATH.read_text())

    sections_out = []
    dates = []
    for sec in cfg["sections"]:
        items_out = []
        for item in sec["items"]:
            row = {k: v for k, v in item.items() if k not in ("source",)}
            src = item.get("source")
            if not src:
                USED["config"] += 1
            else:
                try:
                    res = TRANSFORMS[src["transform"]](src)
                    for k in ("val", "spark", "v", "p", "d", "dir"):
                        if k in res:
                            row[k] = res[k]
                    if res.get("date"):
                        row["_date"] = res["date"].isoformat()
                        dates.append(res["date"])
                except Exception as e:  # keep config value; never blank the page
                    USED["config"] += 1
                    sys.stderr.write("  [skip] %s/%s: %s — kept config value\n"
                                     % (sec["id"], item.get("id"), str(e)[:80]))
            items_out.append(row)
        sections_out.append({"id": sec["id"], "num": sec["num"], "title": sec["title"],
                             "tag": sec["tag"], "kind": sec["kind"], "items": items_out})

    today = dt.datetime.now(dt.timezone.utc).date()
    freshest = max(dates) if dates else today
    out = {
        "masthead": cfg["masthead"],
        "verdict": cfg["verdict"],
        "sections": sections_out,
        "footer": cfg.get("footer", {}),
        "asof": "Updated %s · live via FRED · calibration editorial" % fmt_date(today),
        "data_through": fmt_date(freshest),
        "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "sources": dict(USED),
    }
    OUT_PATH.write_text(json.dumps(out, indent=2, ensure_ascii=False) + "\n")
    print("Wrote %s — sources: %s; data through %s"
          % (OUT_PATH, USED, out["data_through"]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
