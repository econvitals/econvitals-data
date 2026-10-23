#!/usr/bin/env python3
"""
Global Central Bank Stance Matrix — data builder (BIS spine + same-day feeds).

Pulls daily policy-rate history for 12 major central banks from the BIS
"Central bank policy rates" dataset (WS_CBPOL) and writes a single self-contained
`data.json` into tools/central-bank-stance/, which the Lab page reads.

BIS publishes in batches and can trail by a week — and can stop: it has not relayed the
Reserve Bank of India since 2026-07-23. So for five banks — the Fed, ECB, Bank of England
and Bank of Canada from their machine-readable feeds, and the RBI from its own web pages —
`fast_sources.py` tops the history up to the current day before anything is derived (see
`apply_fast`). Every row carries the date it is current through and the source that served
it. The order for any bank is: its own fast source, then BIS, then (only if the config
carries one) a hand entry.

For each bank we derive, straight from the rate history:
  - current rate (the Fed is shown as its 25bp target range around the BIS midpoint)
  - the last move (date, size in bp, direction)
  - the cycle phase (Hiking / On Hold / Cutting / Paused) from the size and
    recency of that last move, with a config override available
  - year-to-date change in bp
  - a compact step-path sparkline (change-points only)

"Next expected move" is a desk view set in cb_matrix_config.yaml; when unset the
page shows an automatic qualitative lean from the phase. BIS has no forward path.

A bank BIS has stopped relaying and no source can serve can carry a `manual_rate` in the
config — the value, its decision date and the source, entered by hand from the central
bank's own release (see `apply_manual`). It is spliced on like a fast feed, only while it
is newer than everything else, and the row says it was entered by hand. It is the
emergency fallback, not a routine: India carried one for a day (2026-10-21) between the
BIS relay stopping and the RBI fast source going in; `--check-stale` fails the run if one
is left unverified for more than STALE_AFTER_DAYS.

Refreshes every 3 hours via .github/workflows/central-bank-stance.yml. BIS and three
of the four fast feeds are keyless; the US target range needs FRED_API_KEY (already a
secret on this repo). Without the key the US row simply falls back to BIS.
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

import fast_sources

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
BASIS_TOL = 0.011       # max |fast - BIS| on BIS's own last date before we refuse to splice
# A hand entry (manual_rate) must not go stale silently. The RBI's MPC meets about every two
# months, so a `verified` date more than this many days old means nobody re-read the bank's
# page after a decision. `--check-stale` exits 1 on it; the workflow runs that AFTER the
# publish, so the matrix still ships and GitHub's failure email is the alarm.
STALE_AFTER_DAYS = 75
# A bank with its own fast source that ends up served by BIS is only fine while BIS is
# current. Past this many days the feed has failed AND BIS is stale (India's relay stopped
# at 2026-07-23), so `--check-stale` fails the run on it too — same alarm, same channel.
FEED_FALLBACK_AFTER_DAYS = 45
# Hand-written text in the config — a desk view (`next_move`) and a phase override
# (`phase_override`) — is a dated opinion. It expires when the bank has made a policy
# decision since it was written, after this many days, or when it is undated. An expired
# desk view is published as "Review pending" (never as stale text) and an expired override
# is dropped so the rule speaks; the build WARNs and `meta.review_pending` names the banks
# for the daily ops mail. Not a `--check-stale` failure: the page is already honest about
# it, and a failing Action every three hours until someone rewrites two sentences is noise.
DESK_VIEW_MAX_AGE_DAYS = 60


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


# ------------------------------------------------------------------------ fast lane
def apply_fast(code: str, series: list[tuple[str, float]]):
    """Splice a same-day feed onto the end of the BIS history for one bank.

    BIS trails by up to a week, so for the four majors with their own machine-readable
    policy rate we append the observations BIS has not caught up to yet. Everything
    downstream (last move, phase, YTD, sparkline) then recomputes untouched.

    The basis is CHECKED, never assumed: the feed must agree with BIS on BIS's own
    last date. A feed that is late, broken or reporting a different concept is dropped
    and the row silently falls back to BIS — today's behavior, not a blank row.
    """
    entry = fast_sources.FAST.get(code)
    if not entry:
        return series, None
    label, fn = entry
    # The feed must cover BIS's own last date to be checked against it, so when BIS has
    # stopped (India: 2026-07-23) the window is widened back to that date.
    start = min(fast_sources.lookback_start(), series[-1][0])
    try:
        fast = fn(start)
    except Exception as e:  # noqa: BLE001
        print(f"[{code}] fast source unavailable ({e!r}); BIS only", file=sys.stderr)
        return series, None
    if not fast:
        return series, None

    bis_date, bis_value = series[-1]
    ref = value_on_or_before(fast, bis_date)
    if ref is None or abs(ref - bis_value) > BASIS_TOL:
        print(f"[{code}] fast source disagrees with BIS on {bis_date} "
              f"({ref} vs {bis_value}); not splicing", file=sys.stderr)
        return series, None

    newer = [(d, v) for d, v in fast if d > bis_date]
    if not newer:
        return series, {"label": label, "through": bis_date, "gained_days": 0}
    gained = (datetime.strptime(newer[-1][0], "%Y-%m-%d").date()
              - datetime.strptime(bis_date, "%Y-%m-%d").date()).days
    return series + newer, {"label": label, "through": newer[-1][0], "gained_days": gained}


# ---------------------------------------------------------------------- hand entry
def apply_manual(code: str, spec: dict, series: list[tuple[str, float]]):
    """Splice a hand-entered rate onto the BIS history for one bank.

    `manual_rate` in cb_matrix_config.yaml carries `value`, `date` (the decision date — the
    RBI's moves take effect the same day, as BIS records them) and `source`, plus `verified`,
    the day the bank's own page was last read showing that value. The entry is used ONLY
    while it is newer than BIS: the moment BIS's last date reaches it, it is ignored and the
    row goes back to BIS untouched, so a stale block retires itself. The move is appended at
    its date and held flat through `verified`, so the row's "through" date is the day a human
    last checked it — not today, which nobody checked.
    """
    m = spec.get("manual_rate")
    if not m:
        return series, None
    date, value = str(m["date"]), float(m["value"])
    verified = str(m.get("verified") or date)
    bis_date = series[-1][0]
    if date <= bis_date:
        print(f"[{code}] the series already runs past the hand entry ({bis_date} >= {date}, "
              f"from BIS or the bank's own source); ignoring manual_rate — delete it from the "
              f"config", file=sys.stderr)
        return series, None
    pts = [(date, value)]
    if verified > date:
        pts.append((verified, value))
    return series + pts, {
        "label": str(m.get("source") or "Entered by hand from the central bank's own release"),
        "url": str(m.get("source_url") or ""),
        "date": date,
        "through": pts[-1][0],
    }


def manual_age_days(spec: dict, today: date):
    """Days since a bank's hand entry was last verified (its `verified`, else its `date`);
    None when the bank has no manual_rate."""
    m = spec.get("manual_rate")
    if not m:
        return None
    v = m.get("verified") or m.get("date")
    v = v if isinstance(v, date) else date.fromisoformat(str(v))
    return (today - v).days


def stale_manual_entries(cfg: dict, today: date, limit_days: int = STALE_AFTER_DAYS) -> list[dict]:
    """Every hand entry older than `limit_days` (strictly: 75 days old is still fresh, 76 is
    stale), as {code, name, verified, age_days}. Pure — reads only the config dict."""
    out = []
    for spec in cfg.get("banks", []):
        age = manual_age_days(spec, today)
        if age is not None and age > limit_days:
            m = spec["manual_rate"]
            out.append({"code": spec["code"], "name": spec["name"],
                        "verified": str(m.get("verified") or m.get("date")), "age_days": age})
    return out


def fallen_back_rows(banks: list[dict], today: date, fast: dict | None = None,
                     limit_days: int = FEED_FALLBACK_AFTER_DAYS) -> list[dict]:
    """Rows in a built data.json whose bank HAS a fast source but was served by BIS this run
    (not fast, not a hand entry), with BIS's last observation more than `limit_days` old —
    the feed failed and BIS is stale. {code, name, source, as_of, age_days}. Pure."""
    fast = fast if fast is not None else fast_sources.FAST
    out = []
    for b in banks:
        if b.get("code") not in fast or b.get("fast") or b.get("manual") or not b.get("as_of"):
            continue
        age = (today - date.fromisoformat(str(b["as_of"]))).days
        if age > limit_days:
            out.append({"code": b["code"], "name": b.get("name", b["code"]),
                        "source": fast[b["code"]][0], "as_of": str(b["as_of"]), "age_days": age})
    return out


def hand_text_expired(edited, last_move_date: str | None, today: date, date_key: str,
                      max_age_days: int = DESK_VIEW_MAX_AGE_DAYS):
    """Why a piece of hand-written config text dated `edited` should no longer be used, or
    None while it stands: undated, written before the bank's most recent policy decision (its
    last-move date from the rate history), or more than `max_age_days` old (60 days old still
    stands, 61 does not). Pure."""
    if not edited:
        return f"undated — no {date_key}"
    ed = edited if isinstance(edited, date) else date.fromisoformat(str(edited))
    if last_move_date and ed < date.fromisoformat(str(last_move_date)):
        return (f"written {ed.isoformat()}, before the bank's latest decision on "
                f"{last_move_date}")
    age = (today - ed).days
    if age > max_age_days:
        return f"written {ed.isoformat()}, {age} days ago (limit {max_age_days})"
    return None


def desk_view_expired(spec: dict, last_move_date: str | None, today: date,
                      max_age_days: int = DESK_VIEW_MAX_AGE_DAYS):
    """Why a bank's hand-written `next_move` should no longer be shown, or None while it stands
    (dated by `next_move_edited`)."""
    if not spec.get("next_move"):
        return None
    return hand_text_expired(spec.get("next_move_edited"), last_move_date, today,
                             "next_move_edited", max_age_days)


def override_expired(spec: dict, last_move_date: str | None, today: date,
                     max_age_days: int = DESK_VIEW_MAX_AGE_DAYS):
    """Why a bank's hand-set `phase_override` should be dropped so the rule speaks, or None
    while it stands (dated by `phase_override_edited`)."""
    if not spec.get("phase_override"):
        return None
    return hand_text_expired(spec.get("phase_override_edited"), last_move_date, today,
                             "phase_override_edited", max_age_days)


def resolve_phase(spec: dict, mv, today: date, lookback_months: int):
    """(phase, phase_source, expired_override_reason): the override while it stands, else the
    rule — classify_phase on the last move. Pure; what main() publishes for the pill."""
    reason = override_expired(spec, mv["date"] if mv else None, today)
    if spec.get("phase_override") and not reason:
        return str(spec["phase_override"]), "override", None
    return classify_phase(mv, today.isoformat(), lookback_months), "auto", reason


def expired_desk_views(banks: list[dict]) -> list[dict]:
    """Rows in a built data.json whose next-move cell is 'Review pending' — the build withheld
    an expired desk view. {code, name, edited, was, reason}. Pure."""
    out = []
    for b in banks:
        nm = b.get("next_move") or {}
        if nm.get("kind") == "pending":
            out.append({"code": b.get("code"), "name": b.get("name", b.get("code")),
                        "edited": nm.get("edited", ""), "was": nm.get("was", ""),
                        "reason": nm.get("reason", "")})
    return out


def expired_overrides(banks: list[dict]) -> list[dict]:
    """Rows whose phase override the build dropped as expired (the pill shows the rule's
    phase; the row carries `override_expired`). {code, name, edited, was, reason}. Pure."""
    out = []
    for b in banks:
        ov = b.get("override_expired")
        if ov:
            out.append({"code": b.get("code"), "name": b.get("name", b.get("code")), **ov})
    return out


def review_pending(banks: list[dict]) -> list[str]:
    """Bank codes with any expired hand-written text — what the daily ops mail reads from
    meta.review_pending. Sorted, unique."""
    codes = {e["code"] for e in expired_desk_views(banks)} | {e["code"] for e in expired_overrides(banks)}
    return sorted(c for c in codes if c)


def check_stale(cfg: dict | None = None, today: date | None = None,
                banks: list[dict] | None = None) -> int:
    """`build.py --check-stale`, the workflow's last step (after the publish): 0 when every hand
    entry is fresh and every bank with its own fast source was served by it or by a current
    BIS; 1 — naming the bank and the dates — when a hand entry is older than STALE_AFTER_DAYS
    or a fast-sourced bank fell back to a BIS observation older than FEED_FALLBACK_AFTER_DAYS.
    Those are DATA-correctness failures and GitHub's failure email is their alarm. Expired desk
    views and overrides are NOT failures: the page already says "Review pending" / shows the
    rule, so they are only counted here and named in meta.review_pending, which the daily ops
    mail reads (jobhost-ops ops_health.check_stance_review_pending)."""
    cfg = cfg if cfg is not None else yaml.safe_load(CONFIG.read_text())
    today = today or datetime.now(timezone.utc).date()
    if banks is None:
        banks = json.loads(OUT.read_text()).get("banks", []) if OUT.exists() else []
    stale = stale_manual_entries(cfg, today)
    fell = fallen_back_rows(banks, today)
    pending = review_pending(banks)
    hands = [s for s in cfg.get("banks", []) if s.get("manual_rate")]
    fed = [b for b in banks if b.get("code") in fast_sources.FAST]
    note = (f"review pending (not a failure; the ops mail reports it): {', '.join(pending)}"
            if pending else "no hand-written text pending review")
    if not stale and not fell:
        print(f"hand entries fresh: {len(hands)} checked, none older than {STALE_AFTER_DAYS} days; "
              f"fast sources: {len(fed)} banks checked, none on a BIS observation older than "
              f"{FEED_FALLBACK_AFTER_DAYS} days; {note} (today {today.isoformat()})")
        return 0
    for e in stale:
        print(f"STALE HAND ENTRY: {e['name']} ({e['code']}) — manual_rate last verified "
              f"{e['verified']}, {e['age_days']} days ago (limit {STALE_AFTER_DAYS}). Re-read the "
              f"bank's own page, update `verified` (and value/date if it moved) in "
              f"cb_matrix_config.yaml; the work order is in NEXT.md.", file=sys.stderr)
    for e in fell:
        print(f"FEED DOWN: {e['name']} ({e['code']}) — its own source ({e['source']}) did not "
              f"serve this run and the row fell back to BIS, whose last observation is "
              f"{e['as_of']}, {e['age_days']} days old (limit {FEED_FALLBACK_AFTER_DAYS}). The "
              f"page publishes that stale value with its date; see the build step's stderr for "
              f"why the source failed (a refused fetch, a changed page, a basis mismatch).",
              file=sys.stderr)
    print(note, file=sys.stderr)
    return 1


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
    manual_entries = []
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

        series, fast = apply_fast(code, series)
        series, manual = apply_manual(code, spec, series)
        if manual:
            manual_entries.append({"code": code, "name": spec["name"], "date": manual["date"],
                                   "through": manual["through"], "source": manual["label"],
                                   "source_url": manual["url"]})
        as_of, rate = series[-1]
        as_of_dates.append(as_of)
        mv = last_move(series)

        phase, phase_source, ov_reason = resolve_phase(spec, mv, date.fromisoformat(today), lookback)
        override_dropped = ({"edited": str(spec.get("phase_override_edited", "") or ""),
                             "was": str(spec["phase_override"]), "reason": ov_reason}
                            if ov_reason else None)

        jan1 = f"{today[:4]}-01-01"
        base = value_on_or_before(series, jan1)
        ytd_bps = round((rate - base) * 100) if base is not None else None

        if spec.get("next_move"):
            edited = str(spec.get("next_move_edited", "") or "")
            reason = desk_view_expired(spec, mv["date"] if mv else None, date.fromisoformat(today))
            if reason:
                # Never publish stale hand text: the cell says so, the alarm says who.
                next_move = {"text": "Review pending", "kind": "pending", "edited": edited,
                             "was": str(spec["next_move"]), "reason": reason}
            else:
                next_move = {"text": str(spec["next_move"]), "kind": "desk", "edited": edited}
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
            "as_of_source": (fast["label"] if fast else manual["label"] if manual else
                             "BIS — Central bank policy rates (WS_CBPOL)"),
            "fast": bool(fast),
            "manual": bool(manual),
            "last_move": (mv | {"display": mv_display}) if mv else None,
            "phase": phase,
            "phase_source": phase_source,
            "override_expired": override_dropped,
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
            "fast_sources": [lbl for lbl, _ in fast_sources.FAST.values()],
            "manual_entries": manual_entries,
            "desk_view_max_age_days": DESK_VIEW_MAX_AGE_DAYS,
            "desk_views_expired": expired_desk_views(banks_out),
            "overrides_expired": expired_overrides(banks_out),
            "review_pending": review_pending(banks_out),
            "source_url": SOURCE_URL,
            "lookback_months": lookback,
            "note": ("Cycle phase is a transparent heuristic derived from the size and "
                     "recency of each bank's last policy-rate change, not an official "
                     "central-bank characterization. “Next move” is either the editor's "
                     "dated desk view or an automatic lean from the phase; hand entries and "
                     "desk views are set by the editor, and BIS publishes no forward path."),
        },
        "summary": {
            "as_of": max(as_of_dates) if as_of_dates else None,
            "as_of_min": min(as_of_dates) if as_of_dates else None,
            "total": len(banks_out),
            "cutting": tally["Cutting"],
            "hold": tally["On Hold"] + tally["Paused"],
            "hiking": tally["Hiking"],
            "paused": tally["Paused"],
        },
        "banks": banks_out,
    }

    OUT.write_text(json.dumps(payload, indent=2, ensure_ascii=False))
    # Publish regardless — the row is dated on the page — but say so; --check-stale fails the run.
    for e in stale_manual_entries(cfg, date.fromisoformat(today)):
        print(f"WARNING stale hand entry: {e['name']} ({e['code']}) verified {e['verified']}, "
              f"{e['age_days']} days ago", file=sys.stderr)
    for e in fallen_back_rows(banks_out, date.fromisoformat(today)):
        print(f"WARNING feed down: {e['name']} ({e['code']}) fell back to BIS at {e['as_of']}, "
              f"{e['age_days']} days old", file=sys.stderr)
    for e in expired_desk_views(banks_out):
        print(f"WARNING desk view expired: {e['name']} ({e['code']}) \"{e['was']}\" "
              f"(edited {e['edited'] or 'undated'}) — {e['reason']}; published as Review pending",
              file=sys.stderr)
    for e in expired_overrides(banks_out):
        print(f"WARNING phase override expired: {e['name']} ({e['code']}) \"{e['was']}\" "
              f"(edited {e['edited'] or 'undated'}) — {e['reason']}; dropped, the rule speaks",
              file=sys.stderr)
    s = payload["summary"]
    fastn = sum(1 for b in banks_out if b.get("fast"))
    handn = sum(1 for b in banks_out if b.get("manual"))
    print(f"wrote {OUT.name} — {s['total']} banks "
          f"({s['cutting']} cutting · {s['hold']} on hold · {s['hiking']} hiking) "
          f"as of {s['as_of_min']}–{s['as_of']}; {fastn} on a same-day feed, "
          f"{handn} entered by hand")


if __name__ == "__main__":
    if "--check-stale" in sys.argv[1:]:
        sys.exit(check_stale())
    main()
