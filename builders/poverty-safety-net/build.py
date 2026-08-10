#!/usr/bin/env python3
"""
Poverty & the Safety Net by State — data build.

Pulls state-level poverty, income, and safety-net-participation figures from the
U.S. Census Bureau public APIs and writes data.json. Also (one-time) bakes the
Albers-projected US-states map geometry into geo.json so the page needs no runtime
map library.

Sources (all Census Bureau, public API — a CENSUS_API_KEY is required by the API):
  - SAIPE (Small Area Income & Poverty Estimates), latest model year:
      poverty rate all ages, child poverty rate (0-17), median household income.
  - ACS 1-year (latest release):
      SNAP household receipt (B22003), SSI household receipt (B19056),
      cash public-assistance household receipt (B19057),
      Medicaid / means-tested public coverage of persons (C27007).

Poverty & program participation for U.S. states is Census territory, not a macro
time-series feed — so this is a legitimate non-Macrobond source. Everything written
to data.json is fetched; nothing is hand-typed.

Env: CENSUS_API_KEY (falls back to ~/.config/macro-dashboard/.env).
Usage: python build.py            # refresh data.json (+ geo.json if missing)
       python build.py --geo      # (re)bake geo.json only
"""
import json, os, sys, urllib.request, urllib.parse, urllib.error
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent.parent  # builders/poverty-safety-net/ -> repo root
DATA_OUT = REPO / "lab" / "poverty-safety-net" / "data.json"
GEO_OUT = REPO / "lab" / "poverty-safety-net" / "geo.json"

# us-atlas states, pre-projected to Albers USA (viewBox ~ 0 0 975 610). Public domain.
GEO_URL = "https://cdn.jsdelivr.net/npm/us-atlas@3/states-albers-10m.json"

# Preferred data years. SAIPE and ACS-1yr both release annually. The build tries this
# year first and automatically steps back up to two years if the vintage isn't published
# yet (empty/HTTP-404 response), so bumping these early is safe.
SAIPE_YEAR = 2024
ACS_YEAR = 2024

STATE_ABBR = {
    "01":"AL","02":"AK","04":"AZ","05":"AR","06":"CA","08":"CO","09":"CT","10":"DE",
    "11":"DC","12":"FL","13":"GA","15":"HI","16":"ID","17":"IL","18":"IN","19":"IA",
    "20":"KS","21":"KY","22":"LA","23":"ME","24":"MD","25":"MA","26":"MI","27":"MN",
    "28":"MS","29":"MO","30":"MT","31":"NE","32":"NV","33":"NH","34":"NJ","35":"NM",
    "36":"NY","37":"NC","38":"ND","39":"OH","40":"OK","41":"OR","42":"PA","44":"RI",
    "45":"SC","46":"SD","47":"TN","48":"TX","49":"UT","50":"VT","51":"VA","53":"WA",
    "54":"WV","55":"WI","56":"WY",
}


def _key():
    k = os.environ.get("CENSUS_API_KEY")
    if k:
        return k
    envf = Path.home() / ".config/macro-dashboard/.env"
    if envf.exists():
        for line in envf.read_text().splitlines():
            if line.startswith("CENSUS_API_KEY="):
                return line.split("=", 1)[1].strip()
    sys.exit("CENSUS_API_KEY not found (env or ~/.config/macro-dashboard/.env)")


def _get(url):
    """GET + parse JSON. Returns None on 204/empty or HTTP 404 (vintage not published)."""
    req = urllib.request.Request(url, headers={"User-Agent": "jasonfurman-lab/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            body = r.read()
    except urllib.error.HTTPError as e:
        if e.code in (404, 204):
            return None
        raise
    if not body.strip():
        return None
    return json.loads(body)


def _fetch_year(fn, year, key, label):
    """Run fn(year, key); step back up to 2 years if a vintage isn't published yet."""
    for y in (year, year - 1, year - 2):
        out = fn(y, key)
        if out:
            if y != year:
                print(f"{label}: {year} not available, used {y}")
            return out, y
    sys.exit(f"{label}: no data for {year}..{year-2}")


def fetch_saipe(year, key):
    """Return {fips: {pov, child_pov, mhi}} from SAIPE for `year`."""
    q = urllib.parse.urlencode({
        "get": "SAEPOVRTALL_PT,SAEPOVRT0_17_PT,SAEMHI_PT",
        "for": "state:*",
        "time": str(year),
        "key": key,
    })
    rows = _get(f"https://api.census.gov/data/timeseries/poverty/saipe?{q}")
    if not rows:
        return {}
    hdr = rows[0]
    out = {}
    for row in rows[1:]:
        d = dict(zip(hdr, row))
        fips = d["state"]
        if fips not in STATE_ABBR:
            continue
        out[fips] = {
            "pov": float(d["SAEPOVRTALL_PT"]),
            "child_pov": float(d["SAEPOVRT0_17_PT"]),
            "mhi": int(d["SAEMHI_PT"]),
        }
    return out


def fetch_acs(year, key):
    """Return {fips: {snap, ssi, pubasst, medicaid}} shares (%) from ACS 1-year."""
    # B22003: SNAP households; B19056: SSI households; B19057: cash public assistance.
    # C27007: Medicaid/means-tested public coverage of persons (split by sex/age).
    medicaid_with = ["C27007_004E", "C27007_007E", "C27007_010E",
                     "C27007_014E", "C27007_017E", "C27007_020E"]
    vars_ = ["B22003_001E", "B22003_002E",
             "B19056_001E", "B19056_002E",
             "B19057_001E", "B19057_002E",
             "C27007_001E"] + medicaid_with
    q = urllib.parse.urlencode({
        "get": "NAME," + ",".join(vars_),
        "for": "state:*",
        "key": key,
    })
    rows = _get(f"https://api.census.gov/data/{year}/acs/acs1?{q}")
    if not rows:
        return {}
    hdr = rows[0]
    out = {}
    for row in rows[1:]:
        d = dict(zip(hdr, row))
        fips = d["state"]
        if fips not in STATE_ABBR:
            continue
        def num(k):
            return float(d[k])
        snap = 100.0 * num("B22003_002E") / num("B22003_001E")
        ssi = 100.0 * num("B19056_002E") / num("B19056_001E")
        pub = 100.0 * num("B19057_002E") / num("B19057_001E")
        med_num = sum(num(k) for k in medicaid_with)
        med = 100.0 * med_num / num("C27007_001E")
        out[fips] = {
            "snap": round(snap, 1),
            "ssi": round(ssi, 1),
            "pubasst": round(pub, 1),
            "medicaid": round(med, 1),
        }
    return out


def fetch_data():
    key = _key()
    saipe, saipe_year = _fetch_year(fetch_saipe, SAIPE_YEAR, key, "SAIPE")
    acs, acs_year = _fetch_year(fetch_acs, ACS_YEAR, key, "ACS")
    states = {}
    for fips, ab in STATE_ABBR.items():
        s = saipe.get(fips, {})
        a = acs.get(fips, {})
        states[fips] = {
            "abbr": ab,
            "pov": s.get("pov"),
            "child_pov": s.get("child_pov"),
            "mhi": s.get("mhi"),
            "snap": a.get("snap"),
            "ssi": a.get("ssi"),
            "pubasst": a.get("pubasst"),
            "medicaid": a.get("medicaid"),
        }
    out = {
        "meta": {
            "source": "U.S. Census Bureau — SAIPE and ACS 1-year public APIs.",
            "saipe_year": saipe_year,
            "acs_year": acs_year,
            "note": "State-level cross-section; figures fetched from Census APIs, not "
                    "necessarily independently checked.",
        },
        "states": states,
    }
    DATA_OUT.write_text(json.dumps(out, indent=2) + "\n")
    # quick sanity: poverty rates should be plausible
    povs = [v["pov"] for v in states.values() if v["pov"] is not None]
    assert povs and all(3 <= p <= 30 for p in povs), "poverty rates out of range"
    assert len([1 for v in states.values() if v["snap"] is not None]) >= 50, "SNAP missing"
    print(f"data.json: {len(states)} states, poverty {min(povs):.1f}-{max(povs):.1f}%")


# ---- geo baking: decode Albers-projected TopoJSON to plain SVG path strings ----

def _decode_arcs(topo):
    """Delta-decode + de-quantize every arc to absolute [x,y] point lists."""
    sx, sy = topo["transform"]["scale"]
    tx, ty = topo["transform"]["translate"]
    arcs = []
    for arc in topo["arcs"]:
        pts, x, y = [], 0, 0
        for dx, dy in arc:
            x += dx
            y += dy
            pts.append((x * sx + tx, y * sy + ty))
        arcs.append(pts)
    return arcs


def _ring_points(arc_idxs, arcs):
    pts = []
    for idx in arc_idxs:
        if idx >= 0:
            seg = arcs[idx]
        else:
            seg = arcs[~idx][::-1]  # negative index => reverse of arc ~idx
        if pts:
            pts.extend(seg[1:])  # drop shared vertex
        else:
            pts.extend(seg)
    return pts


def _path_from_polys(polys, arcs):
    """polys: list of polygons, each a list of rings, each a list of arc indices."""
    d = []
    for rings in polys:
        for ring in rings:
            pts = _ring_points(ring, arcs)
            if not pts:
                continue
            d.append("M" + "L".join(f"{x:.1f},{y:.1f}" for x, y in pts) + "Z")
    return "".join(d)


def bake_geo():
    topo = _get(GEO_URL)
    arcs = _decode_arcs(topo)
    feats = {}
    for g in topo["objects"]["states"]["geometries"]:
        fips = g["id"]
        if fips not in STATE_ABBR:
            continue
        if g["type"] == "Polygon":
            polys = [g["arcs"]]
        elif g["type"] == "MultiPolygon":
            polys = g["arcs"]
        else:
            continue
        feats[fips] = {
            "name": g["properties"]["name"],
            "abbr": STATE_ABBR[fips],
            "d": _path_from_polys(polys, arcs),
        }
    out = {"viewBox": "0 0 975 610", "states": feats}
    GEO_OUT.write_text(json.dumps(out) + "\n")
    print(f"geo.json: {len(feats)} states baked")


if __name__ == "__main__":
    if "--geo" in sys.argv:
        bake_geo()
    else:
        if not GEO_OUT.exists():
            bake_geo()
        fetch_data()
