#!/usr/bin/env python3
"""
Global Central Bank Stance Matrix — data builder (BIS, keyless).

Pulls daily policy-rate history for 12 major central banks from the BIS
"Central bank policy rates" dataset (WS_CBPOL) and writes a single self-contained
`data.json` next to this script, which the Lab page reads.

For each bank we derive, straight from the rate history:
  - current rate (the Fed is shown as its 25bp target range around the BIS midpoint)
  - the last move (date, size in bp, direction)
  - the cycle phase (Hiking / On Hold / Cutting / Paused) from the size and
    recency of that last move, with a config override available
  - year-to-date change in bp
  - a compact step-path sparkline (change-points only)

"Next expected move" is a desk view set in cb_matrix_config.yaml; when unset the
page shows an automatic qualitative lean from the phase. BIS has no forward path.

Refreshes daily via .github/workflows/cb-stance.yml (keyless — no secrets needed).
Run by hand any time with:  python3 build.py

This is an experimental Lab monitor: the phase classification is a transparent
heuristic, not an official central-bank characterization.
"""

from __future__ import annotations

import csv
import io
import json
import sys
import urllib.request
from datetime import date, datetime, timezone
from pathlib import Path

try:
    import yaml
except ImportError:  # pragma: no cover
    sys.exit("PyYAML is required: pip install pyyaml")

HERE = Path(__file__).resolve().parent
REPO = HERE.parent.parent  # builders/central-bank-stance/ -> repo root
CONFIG = HERE / "cb_matrix_config.yaml"
OUT = REPO / "tools" / "central-bank-stance" / "data.json"

BIS_URL = (
    "https://stats.bis.org/api/v2/data/dataflow/BIS/WS_CBPOL/1.0/"
    "D.{area}?startPeriod=2015-01-01&format=csv"
)
SOURCE_URL = "https://data.bis.org/topics/CBPOL"
HIST_YEARS_ON_PAGE = 3  # how much of the step path to ship for the sparkline


# ----------------------------------------------------------------------------- fetch
def fetch_series(area: str) -> list[tuple[str, float]]:
    """Daily BIS policy rate for one REF_AREA -> chronological [(iso_date, value)]."""
    url = BIS_URL.format(area=area)
    req = urllib.request.Request(url, headers={"User-Agent": "jfmacro-lab/1.0"})
    raw = urllib.request.urlopen(req, timeout=60).read().decode("utf-8")
    out: list[tuple[str, float]] = []
    for row in csv.DictReader(io.StringIO(raw)):
        d, v = row.get("TIME_PERIOD"), row.get("OBS_VALUE")
        if not d or v in (None, "", "NaN"):
            continue
        try:
            out.append((d, float(v)))
        except ValueError:
            continue
    out.sort(key=lambda t: t[0])
    return out


# ----------------------------------------------------------------------------- derive
def change_points(series: list[tuple[str, float]]) -> list[tuple[str, float]]:
    """First obs + every date where the level changes (the full step path)."""
    pts: list[tuple[str, float]] = []
    last = None
    for d, v in series:
        if last is None or v != last:
            pts.append((d, v))
            last = v
    return pts


def last_move(series: list[tuple[str, float]]):
    """Most recent change: {date, delta_bps, direction} or None if flat throughout."""
    pts = change_points(series)
    if len(pts) < 2:
        return None
    (d_prev, v_prev), (d_cur, v_cur) = pts[-2], pts[-1]
    delta = round((v_cur - v_prev) * 100)
    return {
        "date": d_cur,
        "delta_bps": delta,
        "direction": "up" if delta > 0 else "down",
    }


def months_between(iso_a: str, iso_b: str) -> float:
    a = datetime.strptime(iso_a, "%Y-%m-%d").date()
    b = datetime.strptime(iso_b, "%Y-%m-%d").date()
    return abs((b - a).days) / 30.44


def value_on_or_before(series, iso: str):
    out = None
    for d, v in series:
        if d <= iso:
            out = v
        else:
            break
    return out


def classify_phase(mv, today: str, lookback_months: int) -> str:
    """Cycle phase from the last move's size and recency."""
    if mv is None:
        return "Paused"
    age = months_between(mv["date"], today)
    if age <= lookback_months:
        return "Cutting" if mv["delta_bps"] < 0 else "Hiking"
    if age <= 12:
        return "On Hold"
    return "Paused"


def fmt_rate(v: float) -> str:
    return f"{v:.2f}%"


def fed_range(mid: float) -> str:
    return f"{mid - 0.125:.2f}–{mid + 0.125:.2f}%"  # 25bp band around the midpoint


def fmt_month(iso: str) -> str:
    return datetime.strptime(iso, "%Y-%m-%d").strftime("%b %Y")


def fmt_bps(n: int) -> str:
    if n == 0:
        return "0 bp"
    return f"{'+' if n > 0 else '−'}{abs(n)} bp"


AUTO_LEAN = {
    "Cutting": "Further cuts likely",
    "Hiking": "Further hikes likely",
    "On Hold": "On hold; data-dependent",
    "Paused": "Extended hold",
}


# ----------------------------------------------------------------------------- main
def main() -> None:
    cfg = yaml.safe_load(CONFIG.read_text())
    lookback = int(cfg.get("lookback_months", 6))
    today = datetime.now(timezone.utc).date().isoformat()

    # prior snapshot, used to keep a row alive if a single fetch fails this run
    prior = {}
    if OUT.exists():
        try:
            prior = {b["code"]: b for b in json.loads(OUT.read_text()).get("banks", [])}
        except Exception:  # noqa: BLE001
            prior = {}

    banks_out = []
    as_of_dates = []
    for spec in cfg["banks"]:
        code = spec["code"]
        try:
            series = fetch_series(code)
            if not series:
                raise ValueError("empty series")
        except Exception as e:  # noqa: BLE001
            print(f"[{code}] fetch failed ({e!r}); keeping prior row", file=sys.stderr)
            if code in prior:
                banks_out.append(prior[code])
            continue

        as_of, rate = series[-1]
        as_of_dates.append(as_of)
        mv = last_move(series)

        phase = spec.get("phase_override") or classify_phase(mv, today, lookback)
        phase_source = "override" if spec.get("phase_override") else "auto"

        jan1 = f"{today[:4]}-01-01"
        base = value_on_or_before(series, jan1)
        ytd_bps = round((rate - base) * 100) if base is not None else None

        if spec.get("next_move"):
            next_move = {"text": str(spec["next_move"]), "kind": "desk",
                         "edited": str(spec.get("next_move_edited", ""))}
        else:
            next_move = {"text": AUTO_LEAN[phase], "kind": "auto", "edited": ""}

        # step path for the sparkline: change-points within the on-page window,
        # always anchored by a starting point and the latest reading
        cutoff = f"{int(today[:4]) - HIST_YEARS_ON_PAGE}-01-01"
        win = [(d, v) for d, v in series if d >= cutoff] or series[-2:]
        path = change_points(win)
        if path[-1][0] != win[-1][0]:
            path.append(win[-1])

        mv_display = None
        if mv:
            arrow = "▲" if mv["direction"] == "up" else "▼"
            mv_display = f"{arrow} {abs(mv['delta_bps'])} bp · {fmt_month(mv['date'])}"

        banks_out.append({
            "code": code,
            "name": spec["name"],
            "country": spec["country"],
            "flag": spec["flag"],
            "instrument": spec["instrument"],
            "rate": round(rate, 3),
            "rate_display": fed_range(rate) if code == "US" else fmt_rate(rate),
            "as_of": as_of,
            "last_move": (mv | {"display": mv_display}) if mv else None,
            "phase": phase,
            "phase_source": phase_source,
            "ytd_bps": ytd_bps,
            "ytd_display": fmt_bps(ytd_bps) if ytd_bps is not None else "—",
            "next_move": next_move,
            "path": [{"date": d, "value": round(v, 3)} for d, v in path],
        })

    tally = {"Cutting": 0, "On Hold": 0, "Hiking": 0, "Paused": 0}
    for b in banks_out:
        tally[b["phase"]] = tally.get(b["phase"], 0) + 1

    payload = {
        "meta": {
            "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "title": "Global Central Bank Stance Matrix",
            "source": "BIS — Central bank policy rates (WS_CBPOL), daily, end of period",
            "source_url": SOURCE_URL,
            "lookback_months": lookback,
            "note": ("Cycle phase is a transparent heuristic derived from the size and "
                     "recency of each bank's last policy-rate change, not an official "
                     "central-bank characterization. “Next move” is a desk view "
                     "set in cb_matrix_config.yaml (or an automatic lean from the phase); "
                     "BIS publishes no forward path."),
        },
        "summary": {
            "as_of": max(as_of_dates) if as_of_dates else None,
            "total": len(banks_out),
            "cutting": tally["Cutting"],
            "hold": tally["On Hold"] + tally["Paused"],
            "hiking": tally["Hiking"],
            "paused": tally["Paused"],
        },
        "banks": banks_out,
    }

    OUT.write_text(json.dumps(payload, indent=2, ensure_ascii=False))
    s = payload["summary"]
    print(f"wrote {OUT.name} — {s['total']} banks "
          f"({s['cutting']} cutting · {s['hold']} on hold · {s['hiking']} hiking) "
          f"as of {s['as_of']}")


if __name__ == "__main__":
    main()
