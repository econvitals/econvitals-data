#!/usr/bin/env python3
"""
fetch_housing.py — daily refresh for the Housing Bubble Monitor lab page.

Reads housing_config.yaml (the editorial layout + per-indicator `source:` specs),
pulls live values, and writes data.json in the shape housing.js renders.

DATA SOURCING — FRED and FHFA, each directly. A series id in a `source:` block is a FRED
series id unless it carries the `fhfa:` prefix, which reads FHFA's own House Price Index
master file (public domain) — see `fhfa_series`. House prices come from FHFA, not FRED,
since 2026-11-02: FRED's Case-Shiller series is tagged "Copyrighted: Pre-approval required"
and the St. Louis Fed asked that it not appear on econvitals.org.

RESILIENCE: every indicator is computed in its own try/except, so the page never
blanks. A failed row is served from last-good.json — the dated value that row last
returned live — and only falls back to the editorial `val`/`spark`/`v` in config
when there is no last-good entry. Every row carries `_source`
(fred / fhfa / last-good / fallback / editorial; `fhfa` = fetched live this run with
FHFA house prices in it, any partner series such as CPI still from FRED) and, where one
exists, `_date`; the
per-run tally goes in data.json's `sources` and the failed keys in `stale_rows`.
`data_through` is computed from real observation dates only, never from today.
Any UNDECLARED failure makes the run exit 1, so a refresh that could not refresh is
reported as failed rather than passing quietly with yesterday's numbers restamped. A
row may DECLARE that it cannot be computed — `known_broken: {reason, until}` on the
config item — in which case it is counted and named every run but does not fail it;
see `_kb_current` and `known_broken_rows` in the output. That is chartbook's
known-broken register in miniature, and it exists for the same reason: an alarm that
is red every night for a fault nobody can fix from here stops being read, and hides
the next real one.

Credentials resolve env var → ~/.config/macro-dashboard/.env (same as the chartbook
clients): FRED_API_KEY. FHFA is keyless.

Run:
  python fetch_housing.py            # writes ./data.json
  HOUSING_OUT=/path/data.json python fetch_housing.py   # cloud Action override
"""
from __future__ import annotations

import csv
import datetime as dt
import io
import json
import re
import os
import sys
import time
from pathlib import Path

import requests
import yaml


# requests puts the FULL keyed URL into its exception text ("400 Client Error ... for url:
# ...?api_key=..."), and the failure reasons below are written into data.json and committed
# to a PUBLIC repo. Take the key out before anything is stored (2026-10-18, pre-launch review).
_KEYED = re.compile(r"(api_key=)[^&\s'\"]+")


def _scrub(text: str) -> str:
    """`text` with any FRED key value replaced by <redacted>."""
    return _KEYED.sub(r"\1<redacted>", text)

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


# ── FHFA House Price Index, direct from FHFA ─────────────────────────────────
# FHFA's one master file (~17 MB, keyless, public domain) carries every HPI series. This
# is a small vendored copy of what macrolib/fhfa.py does for `fhfa:HPI_PO` — the Action has
# no macrolib — and it keeps that module's guards: refuse an HTML page served in place of
# the file, a changed header, or a truncated body, so a broken download raises (and the row
# goes to last-good) instead of parsing into a short or wrong series.
FHFA_MASTER_URL = "https://www.fhfa.gov/hpi/download/monthly/hpi_master.csv"
FHFA_COLUMNS = ["hpi_type", "hpi_flavor", "frequency", "level", "place_name", "place_id",
                "yr", "period", "index_nsa", "index_sa", "rstderr", "note"]
# Config name -> the column read off the U.S. monthly purchase-only rows. Monthly observations
# are dated the first of the month, FRED's convention, so they line up with CPI and rent.
FHFA_SERIES = {"HPI_PO": "index_sa", "HPI_PO_NSA": "index_nsa"}
_fhfa_rows = None


def _fhfa_us_po_monthly():
    global _fhfa_rows
    if _fhfa_rows is not None:
        return _fhfa_rows
    r = SESSION.get(FHFA_MASTER_URL, timeout=180, headers={
        "User-Agent": "econvitals-data housing-monitor (+https://github.com/econvitals/econvitals-data)"})
    r.raise_for_status()
    text = r.content.decode("utf-8-sig", errors="replace")
    first = text[:4096].split("\n", 1)[0].strip()
    if first.lower().startswith("<") or "<html" in text[:4096].lower():
        raise RuntimeError("FHFA served an HTML page, not the HPI master CSV — the file moved?")
    if [c.strip() for c in first.split(",")] != FHFA_COLUMNS:
        raise RuntimeError("FHFA HPI master CSV header changed: %r" % first[:200])
    if text.count("\n") < 100_000:
        raise RuntimeError("FHFA HPI master CSV is truncated (%d lines)" % text.count("\n"))
    rows = [row for row in csv.DictReader(io.StringIO(text))
            if row["hpi_type"] == "traditional" and row["hpi_flavor"] == "purchase-only"
            and row["frequency"] == "monthly" and row["place_id"].strip() == "USA"]
    if len(rows) < 300:
        raise RuntimeError("FHFA HPI master CSV: only %d U.S. monthly purchase-only rows" % len(rows))
    _fhfa_rows = rows
    return rows


def fhfa_series(name):
    """FHFA U.S. purchase-only HPI, monthly (Jan 1991 = 100): `HPI_PO` seasonally
    adjusted, `HPI_PO_NSA` not. Returns sorted list[(date, value)]."""
    col = FHFA_SERIES.get(name)
    if col is None:
        raise RuntimeError("unknown FHFA series %r (one of %s)" % (name, ", ".join(FHFA_SERIES)))
    obs = []
    for row in _fhfa_us_po_monthly():
        v = (row.get(col) or "").strip()
        if v in ("", "-", ".", "NA"):
            continue
        obs.append((dt.date(int(row["yr"]), int(row["period"]), 1), float(v.replace(",", ""))))
    obs.sort(key=lambda o: o[0])
    if not obs:
        raise RuntimeError("FHFA %s: no observations" % name)
    return obs


# ── resolver ─────────────────────────────────────────────────────────────────
# fred      — FRED series fetched live this run
# fhfa      — FHFA house-price series fetched live this run (FHFA's own file, not FRED)
# config    — rows served from the editorial value (no source block, or a failed
#             fetch with nothing in the last-good store)
# last_good — rows served from a previously fetched, dated value
# failed    — wired rows whose fetch raised this run (last_good + fallback rows)
# known_broken — rows that raised AND said so in advance: the config item carries a
#             `known_broken: {reason, until}` block. Counted and printed every run,
#             never counted as `failed`, so a fault nobody can fix from here stops
#             turning the nightly run red while a NEW break still does. Same register
#             as chartbook's render/known_broken_charts.txt, and the same rule: it is a
#             DEBT LIST, not a dismissal — `until` is a review date and the run fails
#             again once it passes, so nothing can quietly become permanent.
USED = {"fred": 0, "fhfa": 0, "config": 0, "last_good": 0, "failed": 0, "known_broken": 0}
# Providers the row being computed has read from (reset per row in main), so a row with an
# FHFA house-price series in it is tagged `_source: "fhfa"` rather than "fred".
_ROW_PROVIDERS = set()

VALUE_KEYS = ("val", "spark", "v", "p", "d", "dir")


def _kb_current(kb):
    """Is this `known_broken:` declaration still in force?

    A declaration needs a `reason` — an entry you cannot write a reason for is a fault,
    not an exception — and an `until` review date in ISO form. Past that date, or with
    either field missing or unparseable, the declaration does NOT hold and the row counts
    as a plain failure again. That is deliberate: the failure mode of every register like
    this one is that it becomes permanent because nothing ever asks again.
    """
    if not str(kb.get("reason") or "").strip():
        return False
    try:
        return dt.date.today() <= dt.date.fromisoformat(str(kb.get("until")).strip())
    except (TypeError, ValueError):
        return False


def get_series(key):
    """Series for a config key — `fhfa:<name>` reads FHFA's master file, anything else is
    a FRED series id. Returns sorted list[(date, value)]. Raises if the source yields
    nothing, which the caller turns into "serve last-good, else the config value"."""
    if str(key).startswith("fhfa:"):
        obs = fhfa_series(key[len("fhfa:"):])
        USED["fhfa"] += 1
        _ROW_PROVIDERS.add("fhfa")
        return obs
    obs = fred_series(key)
    USED["fred"] += 1
    _ROW_PROVIDERS.add("fred")
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
    hp = get_series(src["series"])
    cpi = get_series(src["deflator"])
    real = [(d, v / val_on_or_before(cpi, d)) for d, v in hp if val_on_or_before(cpi, d)]
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
    # Carried forward, refreshed row by row — but only for rows the config still wires, so
    # a row that is renamed or re-sourced does not leave its old value in the public store.
    wired_keys = {"%s/%s" % (sec["id"], item.get("id"))
                  for sec in cfg["sections"] for item in sec["items"] if item.get("source")}
    fresh_good = {k: v for k, v in last_good.items() if k in wired_keys}

    sections_out = []
    dates = []
    failures = []
    reasons = {}
    declared = []
    declared_reasons = {}
    wired = 0
    for sec in cfg["sections"]:
        items_out = []
        for item in sec["items"]:
            row = {k: v for k, v in item.items()
                   if k not in ("source", "known_broken")}
            src = item.get("source")
            key = "%s/%s" % (sec["id"], item.get("id"))
            if not src:
                USED["config"] += 1
                row["_source"] = "editorial"
            else:
                wired += 1
                _ROW_PROVIDERS.clear()
                try:
                    res = TRANSFORMS[src["transform"]](src)
                    keep = {k: res[k] for k in VALUE_KEYS if k in res}
                    row.update(keep)
                    row["_source"] = "fhfa" if "fhfa" in _ROW_PROVIDERS else "fred"
                    if res.get("date"):
                        row["_date"] = res["date"].isoformat()
                        dates.append(res["date"])
                        keep["date"] = row["_date"]
                    fresh_good[key] = keep
                except Exception as e:
                    # Never blank the page — but never republish a stale number as
                    # today's live print either. Serve the dated last-good value if
                    # we have one, else the editorial placeholder, and say which.
                    kb = item.get("known_broken")
                    if isinstance(kb, dict) and _kb_current(kb):
                        USED["known_broken"] += 1
                        declared.append(key)
                        declared_reasons[key] = str(kb.get("reason") or "")[:200]
                    else:
                        USED["failed"] += 1
                        failures.append(key)
                        reasons[key] = _scrub(str(e))[:200]
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
                    tag = "known-broken" if key in declared else "skip"
                    sys.stderr.write("  [%s] %s: %s — %s\n"
                                     % (tag, key, str(e)[:80], held))
            items_out.append(row)
        sections_out.append({"id": sec["id"], "num": sec["num"], "title": sec["title"],
                             "tag": sec["tag"], "kind": sec["kind"], "items": items_out})

    today = dt.datetime.now(dt.timezone.utc).date()
    # data_through is a claim about DATA, so it comes only from real observation
    # dates (live or last-good). With nothing dated it stays empty and the page
    # renders "—"; it must never fall back to today's wall clock.
    freshest = max(dates) if dates else None
    asof = "Updated %s · live via FRED and FHFA · calibration editorial" % fmt_date(today)
    held_n = USED["failed"] + USED["known_broken"]
    if held_n:
        asof = ("Updated %s · %d of %d live indicators unavailable, held at last "
                "reported value · calibration editorial"
                % (fmt_date(today), held_n, wired))
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
        # The declared register, published beside the undeclared failures so the debt is
        # visible to anyone reading the file rather than only to whoever opens the config.
        "known_broken_rows": declared,
        "known_broken_reasons": declared_reasons,
    }
    OUT_PATH.write_text(json.dumps(out, indent=2, ensure_ascii=False) + "\n")
    save_last_good(fresh_good)
    print("Wrote %s — sources: %s; data through %s"
          % (OUT_PATH, USED, out["data_through"] or "(none)"))
    if declared:
        # Counted and named every run, exactly like chartbook's known-broken register: the
        # file being empty is the goal state, and a row here that has quietly become fixable
        # should come off it.
        sys.stderr.write("known-broken (declared, not failing the run) — %d row(s): %s\n"
                         % (len(declared), ", ".join(declared)))
    if failures:
        # The page is still serviceable, but the refresh did not do its job — fail
        # the run so the Action reports red instead of a silent green every night.
        sys.stderr.write("FETCH FAILED for %d row(s): %s\n"
                         % (len(failures), ", ".join(failures)))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
