#!/usr/bin/env python3
"""
fetch_housing.py — daily refresh for the Housing Bubble Monitor lab page.

Reads housing_config.yaml (the editorial layout + per-indicator `source:` specs),
pulls live values, and writes data.json in the shape housing.js renders.

DATA SOURCING — FRED, directly. Every config key that has a `source:` block is a FRED
series id; there is one fetch path and no vendor in front of it.

RESILIENCE: every indicator is computed in its own try/except, so the page never
blanks. A failed row is served from last-good.json — the dated value that row last
returned live — and only falls back to the editorial `val`/`spark`/`v` in config
when there is no last-good entry. Every row carries `_source`
(fred / last-good / fallback / editorial) and, where one exists, `_date`; the
per-run tally goes in data.json's `sources` and the failed keys in `stale_rows`.
`data_through` is computed from real observation dates only, never from today.
Any failure makes the run exit 1, so a refresh that could not refresh is reported
as failed rather than passing quietly with yesterday's numbers restamped.

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
# Last-good store, beside the output and committed with it: the dated value each
# wired row last returned live. A failed fetch reads from here instead of silently
# republishing the editorial placeholder as though it were today's FRED print.
LAST_GOOD_PATH = OUT_PATH.parent / "last-good.json"
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
# fred      — series fetched live this run
# config    — rows served from the editorial value (no source block, or a failed
#             fetch with nothing in the last-good store)
# last_good — rows served from a previously fetched, dated value
# failed    — wired rows whose fetch raised this run (last_good + fallback rows)
USED = {"fred": 0, "config": 0, "last_good": 0, "failed": 0}

VALUE_KEYS = ("val", "spark", "v", "p", "d", "dir")


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
    year = int(src["base_year"])
    base = mean_in_year(obs, year)
    if not base:
        # NOT A FETCH FAILURE, AND NOT THE API KEY. The series arrived; it simply no longer
        # reaches back to the base year. FRED truncated every NAR series to a rolling 13
        # months on 2026-08-11 (HOSMEDUSM052N now starts 2025-07, and ALFRED has no earlier
        # vintage either), so this row cannot be computed from FRED at all any more. Say that
        # here, because a bare TypeError sent the ops mail chasing the FRED key.
        raise ValueError("%s no longer carries %d (it starts %s) — the source was truncated, "
                         "so this row cannot be computed from FRED"
                         % (src["series"], year, obs[0][0].isoformat() if obs else "nowhere"))
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


# ── last-good store ──────────────────────────────────────────────────────────
def load_last_good():
    """{"section/item": {"date": iso, <value keys>}} — missing/corrupt reads empty."""
    try:
        blob = json.loads(LAST_GOOD_PATH.read_text())
        return blob.get("rows", {}) if isinstance(blob, dict) else {}
    except Exception:
        return {}


def save_last_good(rows):
    LAST_GOOD_PATH.write_text(json.dumps(
        {"note": "Last live value per wired row; read by fetch_housing.py when a "
                 "fetch fails. Generated — do not hand-edit.",
         "rows": dict(sorted(rows.items()))},
        indent=2, ensure_ascii=False) + "\n")


# ── main ───────────────────────────────────────────────────────────────────--
def main():
    cfg = yaml.safe_load(CONFIG_PATH.read_text())

    last_good = load_last_good()
    fresh_good = dict(last_good)   # carried forward, refreshed row by row

    sections_out = []
    dates = []
    failures = []
    reasons = {}
    wired = 0
    for sec in cfg["sections"]:
        items_out = []
        for item in sec["items"]:
            row = {k: v for k, v in item.items() if k not in ("source",)}
            src = item.get("source")
            key = "%s/%s" % (sec["id"], item.get("id"))
            if not src:
                USED["config"] += 1
                row["_source"] = "editorial"
            else:
                wired += 1
                try:
                    res = TRANSFORMS[src["transform"]](src)
                    keep = {k: res[k] for k in VALUE_KEYS if k in res}
                    row.update(keep)
                    row["_source"] = "fred"
                    if res.get("date"):
                        row["_date"] = res["date"].isoformat()
                        dates.append(res["date"])
                        keep["date"] = row["_date"]
                    fresh_good[key] = keep
                except Exception as e:
                    # Never blank the page — but never republish a stale number as
                    # today's live print either. Serve the dated last-good value if
                    # we have one, else the editorial placeholder, and say which.
                    USED["failed"] += 1
                    failures.append(key)
                    reasons[key] = str(e)[:200]
                    prev = last_good.get(key)
                    if prev:
                        USED["last_good"] += 1
                        row.update({k: v for k, v in prev.items() if k in VALUE_KEYS})
                        row["_source"] = "last-good"
                        if prev.get("date"):
                            row["_date"] = prev["date"]
                            dates.append(dt.date.fromisoformat(prev["date"]))
                        held = "held at last-good %s" % (prev.get("date") or "(undated)")
                    else:
                        USED["config"] += 1
                        row["_source"] = "fallback"
                        held = "no last-good — kept config value"
                    sys.stderr.write("  [skip] %s: %s — %s\n"
                                     % (key, str(e)[:80], held))
            items_out.append(row)
        sections_out.append({"id": sec["id"], "num": sec["num"], "title": sec["title"],
                             "tag": sec["tag"], "kind": sec["kind"], "items": items_out})

    today = dt.datetime.now(dt.timezone.utc).date()
    # data_through is a claim about DATA, so it comes only from real observation
    # dates (live or last-good). With nothing dated it stays empty and the page
    # renders "—"; it must never fall back to today's wall clock.
    freshest = max(dates) if dates else None
    asof = "Updated %s · live via FRED · calibration editorial" % fmt_date(today)
    if failures:
        asof = ("Updated %s · %d of %d live indicators unavailable, held at last "
                "reported value · calibration editorial"
                % (fmt_date(today), USED["failed"], wired))
    out = {
        "masthead": cfg["masthead"],
        "verdict": cfg["verdict"],
        "sections": sections_out,
        "footer": cfg.get("footer", {}),
        "asof": asof,
        "data_through": fmt_date(freshest),
        "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "sources": dict(USED),
        "stale_rows": failures,
        # WHY each one failed, not just which: the daily ops check reads this and can then name
        # the real cause instead of telling a reader to go and look at the FRED key.
        "stale_reasons": reasons,
    }
    OUT_PATH.write_text(json.dumps(out, indent=2, ensure_ascii=False) + "\n")
    save_last_good(fresh_good)
    print("Wrote %s — sources: %s; data through %s"
          % (OUT_PATH, USED, out["data_through"] or "(none)"))
    if failures:
        # The page is still serviceable, but the refresh did not do its job — fail
        # the run so the Action reports red instead of a silent green every night.
        sys.stderr.write("FETCH FAILED for %d row(s): %s\n"
                         % (len(failures), ", ".join(failures)))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
