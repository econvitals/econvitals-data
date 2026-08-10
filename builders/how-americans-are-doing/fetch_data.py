#!/usr/bin/env python3
"""
Build data.json for the "How Americans Are Doing" Lab dashboard.

Two data sources are merged:
  * LIVE  — standard public series (BEA per-capita, Census income & Gini,
            World Bank cross-country) pulled fresh from FRED. These are the
            series that actually move between monthly updates.
  * CURATED — series NOT available live (IPUMS/AHS housing quality, CDC cause-
            of-death, EPA/FBI/NHTSA, Columbia SPM, ITU tech), transcribed from
            Jason's frozen Empirical Briefing artifacts. Read from curated.json;
            these change only when the underlying EB is re-run.

The SECTIONS layout below is the single source of truth: it lists every section,
every chart, and every series, tagging each series as live (a FRED code) or
curated (a key in curated.json). Run:  python3 fetch_data.py

No timestamp is written into data.json (only real data dates), so a no-op run
produces no diff and the monthly updater makes no spurious commit.

Pure Python standard library (urllib, json) — no pip installs — so it runs
identically on a laptop and in GitHub Actions. The house rule is the agency direct
(via macrolib), FRED as backup; these particular series are World Bank / Census / BEA
series that FRED serves first-class and that the EBs themselves pulled "via FRED", so
FRED is used here for reproducibility and cloud-simplicity — Actions can't import
macrolib. See run-note; they can be repointed at the agency feeds.

A few CURATED charts below carry "via Macrobond" in their `source:` text. That is the
credit line printed on the chart, recording where a frozen number originally came from —
not a live connection. Macrobond was retired 2026-07-25 and nothing in this file calls it.
Leave those labels as they are; rewriting them would misstate the provenance, which is
documented in curated_SOURCES.md.
"""
import json, os, sys, time, urllib.parse, urllib.request
from collections import defaultdict
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent.parent  # builders/how-americans-are-doing/ -> repo root
OUT_DIR = REPO / "lab" / "how-americans-are-doing"  # curated.json in, data.json out
FRED = "https://api.stlouisfed.org/fred/series/observations"


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
def fred_raw(code, start="1947-01-01"):
    """Return list of (year:int, month:int, value:float) for a FRED series."""
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
                y, m = int(o["date"][:4]), int(o["date"][5:7])
                out.append((y, m, float(v)))
            if not out:
                raise RuntimeError(f"{code}: no observations")
            return out
        except Exception as e:  # noqa
            if attempt == 3:
                raise
            time.sleep(1.5 * (attempt + 1))


def to_annual(raw, how="mean"):
    """Collapse (y,m,v) rows to [[year, value], ...]. how = mean | last."""
    by_year = defaultdict(list)
    for y, m, v in raw:
        by_year[y].append((m, v))
    out = []
    for y in sorted(by_year):
        pts = sorted(by_year[y])
        val = pts[-1][1] if how == "last" else sum(v for _, v in pts) / len(pts)
        out.append([y, round(val, 3)])
    return out


_CACHE = {}


def live(code, start="1947-01-01", how="mean"):
    ck = (code, start, how)
    if ck not in _CACHE:
        _CACHE[ck] = to_annual(fred_raw(code, start), how)
    # return a copy so callers can't mutate the cache
    return [list(p) for p in _CACHE[ck]]


def deflate_to_2025(nominal, ref_year=2025):
    """Deflate an annual [[y,nominal]] series to ref-year dollars using PCEPI."""
    pce = {y: v for y, v in live("PCEPI", start="1960-01-01")}
    if ref_year not in pce:
        ref_year = max(pce)
    ref = pce[ref_year]
    out = []
    for y, v in nominal:
        if y in pce:
            out.append([y, round(v * ref / pce[y], 1)])
    return out


def real_ahe():
    """Real avg hourly earnings, production & nonsupervisory, in 2025 dollars."""
    return deflate_to_2025(live("AHETPI", start="1964-01-01", how="mean"))


# ----------------------------------------------------------------------------- layout
# direction: "up" = higher is better, "down" = lower is better, "neutral".
# Each series is either {"live": "<FRED>", ...} or {"curated": "<key>"}.
SECTIONS = [
    {
        "id": "income", "title": "Income, Work & Consumption",
        "blurb": "The material core: what a typical American family earns, spends, "
                 "and how many hours they work for it. Deflated to today's dollars, "
                 "the long climb is unmistakable — with a visible stall in the 1970s "
                 "and again around the 2008 crisis.",
        "charts": [
            {"id": "median-family-income", "title": "Real median family income",
             "units": "2024 dollars", "direction": "up",
             "source": "Census (via FRED, MEFAINUSA672N)",
             "note": "The income of the family right in the middle of the distribution, adjusted for inflation.",
             "series": [{"name": "Real median family income", "live": "MEFAINUSA672N", "how": "annual", "start": "1953-01-01"}]},
            {"id": "median-household-income", "title": "Real median household income",
             "units": "2024 dollars", "direction": "up",
             "source": "Census (via FRED, MEHOINUSA672N)",
             "note": "Households (incl. single-person) run lower than families; series starts 1984.",
             "series": [{"name": "Real median household income", "live": "MEHOINUSA672N", "how": "annual", "start": "1984-01-01"}]},
            {"id": "disposable-income-pc", "title": "Real disposable income per person",
             "units": "chained dollars", "direction": "up",
             "source": "BEA (via FRED, A229RX0)",
             "note": "After-tax income per person, inflation-adjusted.",
             "series": [{"name": "Real disposable income per capita", "live": "A229RX0", "how": "mean", "start": "1959-01-01"}]},
            {"id": "consumption-pc", "title": "Real consumption per person",
             "units": "chained dollars", "direction": "up",
             "source": "BEA (via FRED, A794RX0Q048SBEA)",
             "note": "Personal consumption spending per person, inflation-adjusted.",
             "series": [{"name": "Real personal consumption per capita", "live": "A794RX0Q048SBEA", "how": "mean", "start": "1947-01-01"}]},
            {"id": "gdp-pc", "title": "Real GDP per person",
             "units": "chained dollars", "direction": "up",
             "source": "BEA (via FRED, A939RX0Q048SBEA)",
             "note": "The broadest measure of output per American.",
             "series": [{"name": "Real GDP per capita", "live": "A939RX0Q048SBEA", "how": "mean", "start": "1947-01-01"}]},
            {"id": "real-wages", "title": "Real wages (production & nonsupervisory workers)",
             "units": "2025 dollars per hour", "direction": "up",
             "source": "BLS AHETPI deflated by PCE (via FRED)",
             "note": "Average hourly earnings of production/nonsupervisory workers, deflated to 2025 dollars. Rose, fell through the 1970s–90s, and has climbed since.",
             "series": [{"name": "Real average hourly earnings", "special": "real_ahe"}]},
            {"id": "durables-pc", "title": "Real durable-goods spending per person",
             "units": "real dollars", "direction": "up",
             "source": "BEA (from EB; via Macrobond)",
             "note": "Cars, appliances, electronics — the fastest-growing slice of consumption (+900% since 1970).",
             "series": [{"name": "Real durables per capita", "curated": "durables_percap"}]},
            {"id": "weekly-hours", "title": "Average weekly hours worked",
             "units": "hours per week", "direction": "down",
             "source": "BLS (from EB; via Macrobond)",
             "note": "The work week has drifted down over 60 years — more of the gains taken as leisure.",
             "series": [{"name": "Average weekly hours", "curated": "weekly_hours"}]},
            {"id": "connectivity", "title": "Technology in the home: mobile & internet",
             "units": "per 100 people / percent of individuals", "direction": "up",
             "source": "ITU / World Bank (from EB; via Macrobond)",
             "note": "From near-zero in the 1980s–90s to near-universal — mobile subscriptions and internet use.",
             "series": [
                 {"name": "Mobile subscriptions per 100", "curated": "mobile_per100"},
                 {"name": "Internet users (%)", "curated": "internet_pct"}]},
            {"id": "women-lfp", "title": "Women's labor-force participation",
             "units": "percent of women 16+", "direction": "neutral",
             "source": "BLS (via FRED, LNS11300002)",
             "note": "The single biggest change in who works: from ~34% in 1948 toward ~57%.",
             "series": [{"name": "Women's participation rate", "live": "LNS11300002", "how": "mean", "start": "1948-01-01"}]},
            {"id": "unemployment", "title": "Unemployment rate",
             "units": "percent", "direction": "down",
             "source": "BLS (via FRED, UNRATE)",
             "note": "Annual average. Context for the income series — recessions are where the stalls happen.",
             "series": [{"name": "Unemployment rate", "live": "UNRATE", "how": "mean", "start": "1948-01-01"}]},
        ],
    },
    {
        "id": "health", "title": "Health",
        "blurb": "Americans live far longer than in 1970 and babies die far less often — "
                 "but the gains stalled in the 2010s, reversed hard in the pandemic, and "
                 "a drug-overdose epidemic and rising maternal deaths cut the other way.",
        "charts": [
            {"id": "life-expectancy", "title": "Life expectancy at birth",
             "units": "years", "direction": "up",
             "source": "World Bank (US total via FRED; sex splits from EB)",
             "note": "Up ~8 years since 1970. The 2020–21 drop is COVID; women still outlive men by ~5 years.",
             "series": [
                 {"name": "Total", "live": "SPDYNLE00INUSA", "how": "annual", "start": "1960-01-01"},
                 {"name": "Male", "curated": "le_male"},
                 {"name": "Female", "curated": "le_female"}]},
            {"id": "infant-mortality", "title": "Infant & child mortality",
             "units": "deaths per 1,000 live births", "direction": "down",
             "source": "World Bank (from EB)",
             "note": "One of the great successes: infant deaths fell ~72% since 1970.",
             "series": [
                 {"name": "Infant (under 1)", "curated": "infant_mortality"},
                 {"name": "Under-5", "curated": "under5_mortality"}]},
            {"id": "maternal-mortality", "title": "Maternal mortality",
             "units": "deaths per 100,000 live births", "direction": "down",
             "source": "World Bank modeled (from EB)",
             "note": "The exception to the health story: US maternal deaths rose over the 2000s–2010s and spiked in 2021.",
             "series": [{"name": "Maternal mortality (modeled)", "curated": "maternal_mortality"}]},
            {"id": "cause-of-death", "title": "Death rates by leading cause",
             "units": "age-adjusted, per 100,000", "direction": "down",
             "source": "CDC/NCHS; overdose from CDC WONDER (from EB)",
             "note": "Heart disease and stroke deaths collapsed. Drug overdose is the one racing upward.",
             "series": [
                 {"name": "Heart disease", "curated": "death_heart"},
                 {"name": "Cancer", "curated": "death_cancer"},
                 {"name": "Stroke", "curated": "death_stroke"},
                 {"name": "Drug overdose", "curated": "death_overdose"},
                 {"name": "Suicide", "curated": "death_suicide"}]},
        ],
    },
    {
        "id": "housing", "title": "Housing",
        "blurb": "Homes got dramatically better: indoor plumbing became universal, "
                 "crowding fell by two-thirds, and space, air conditioning and appliances "
                 "spread to nearly everyone. Quality is a triumph even where affordability isn't.",
        "charts": [
            {"id": "plumbing-kitchen", "title": "Homes lacking complete plumbing or kitchen",
             "units": "percent of homes", "direction": "down",
             "source": "IPUMS Census/ACS (from EB)",
             "note": "In 1960, 1 in 7 homes lacked complete plumbing. Today it is essentially gone.",
             "series": [
                 {"name": "Lacking complete plumbing", "curated": "housing_plumbing"},
                 {"name": "Lacking complete kitchen", "curated": "housing_kitchen"}]},
            {"id": "crowding", "title": "Overcrowding",
             "units": "percent of homes (>1 person per room)", "direction": "down",
             "source": "IPUMS Census/ACS (from EB)",
             "note": "Overcrowding fell from ~11% in 1960 to ~3.5%.",
             "series": [{"name": "Overcrowded homes", "curated": "housing_overcrowd"}]},
            {"id": "space", "title": "Space per person",
             "units": "count per person", "direction": "up",
             "source": "IPUMS Census/ACS (from EB)",
             "note": "Bedrooms and rooms per person have risen steadily — more room even as families shrank.",
             "series": [
                 {"name": "Bedrooms per person", "curated": "housing_bedrooms_pp"},
                 {"name": "Rooms per person", "curated": "housing_rooms_pp"}]},
            {"id": "spare-bedroom", "title": "Families with a spare bedroom",
             "units": "percent of families", "direction": "up",
             "source": "IPUMS Census/ACS (from EB)",
             "note": "A majority of families now have more bedrooms than they strictly need.",
             "series": [{"name": "Families with a spare bedroom", "curated": "housing_spare_bed"}]},
            {"id": "new-home-size", "title": "Size of a new single-family home",
             "units": "median square feet", "direction": "neutral",
             "source": "Census, Characteristics of New Housing (from EB)",
             "note": "New homes grew ~40% larger from the late 1980s to the mid-2010s, then edged back.",
             "series": [{"name": "Median new-home sq ft", "curated": "newhome_sqft"}]},
            {"id": "amenities", "title": "Home amenities",
             "units": "percent of homes", "direction": "up",
             "source": "American Housing Survey (from EB)",
             "note": "AHS microdata only runs 2015+, but the levels are near-universal. Air conditioning is up ~675% since 1970 (see summary).",
             "series": [
                 {"name": "Air conditioning (any)", "curated": "housing_ac_any"},
                 {"name": "Central air", "curated": "housing_ac_central"},
                 {"name": "Dishwasher", "curated": "housing_dishwasher"},
                 {"name": "Clothes dryer", "curated": "housing_dryer"}]},
        ],
    },
    {
        "id": "safety-env", "title": "Safety & Environment",
        "blurb": "The air is far cleaner, the roads far safer, and property crime is down "
                 "by half — even as violent crime and, above all, drug overdoses moved the "
                 "wrong way. The environment is the clearest win of the last half-century.",
        "charts": [
            {"id": "air", "title": "Air pollution",
             "units": "emissions index, 1970 = 100", "direction": "down",
             "source": "EPA National Emissions Inventory (from EB)",
             "note": "Combined emissions of the six criteria pollutants fell ~73% even as the economy tripled.",
             "series": [{"name": "Air pollutant emissions", "curated": "air_emissions"}]},
            {"id": "crime", "title": "Crime rates",
             "units": "offenses per 100,000", "direction": "down",
             "source": "FBI UCR (from EB)",
             "note": "Both crime rates peaked around 1990. Property crime kept falling; violent crime is roughly back to its 1970 level.",
             "series": [
                 {"name": "Violent crime", "curated": "crime_violent"},
                 {"name": "Property crime", "curated": "crime_property"}]},
            {"id": "traffic", "title": "Traffic fatalities",
             "units": "per 100M vehicle miles / per 100k people", "direction": "down",
             "source": "NHTSA / FARS (from EB)",
             "note": "Deaths per mile driven fell ~73% — seatbelts, airbags, safer cars and roads.",
             "series": [
                 {"name": "Deaths per 100M miles", "curated": "traffic_per_mile"},
                 {"name": "Deaths per 100k people", "curated": "traffic_per_100k"}]},
            {"id": "overdose-vs-traffic", "title": "The one that reversed: drug overdoses",
             "units": "age-adjusted deaths per 100,000", "direction": "down",
             "source": "CDC WONDER (from EB)",
             "note": "Set against every other safety gain, overdose deaths are up more than fivefold since 1999.",
             "series": [{"name": "Drug overdose deaths", "curated": "death_overdose"}]},
        ],
    },
    {
        "id": "poverty-inequality", "title": "Poverty, Inequality & Opportunity",
        "blurb": "Measured against a fixed standard of living, poverty has fallen by more "
                 "than half since the 1960s and education has soared. Income inequality, by "
                 "contrast, rose from the 1970s and remains near its highs.",
        "charts": [
            {"id": "anchored-poverty", "title": "Poverty against a fixed standard (anchored SPM)",
             "units": "percent in poverty", "direction": "down",
             "source": "Columbia Center on Poverty & Social Policy (from EB)",
             "note": "Holding the poverty line at a fixed real level and counting taxes and transfers, poverty fell from ~26% (1967) to ~10% (2024). (Two anchor points shown.)",
             "series": [{"name": "Anchored SPM poverty", "curated": "anchored_spm"}]},
            {"id": "official-poverty", "title": "Official poverty rate",
             "units": "percent in poverty", "direction": "down",
             "source": "Census (via FRED, PPAAUS00000A156NCEN)",
             "note": "The official rate (a relative-ish measure) has hovered in a band; series available from 1989.",
             "series": [{"name": "Official poverty rate", "live": "PPAAUS00000A156NCEN", "how": "annual", "start": "1989-01-01"}]},
            {"id": "gini", "title": "Income inequality (Gini)",
             "units": "Gini index (0 = equal, 1 = unequal)", "direction": "down",
             "source": "Census (via FRED, GINIALLRF / GINIALLRH)",
             "note": "Inequality among families and households has risen since the late 1960s. Higher = more unequal.",
             "series": [
                 {"name": "Families", "live": "GINIALLRF", "how": "annual", "start": "1947-01-01"},
                 {"name": "Households", "live": "GINIALLRH", "how": "annual", "start": "1967-01-01"}]},
            {"id": "education", "title": "Educational attainment (age 25+)",
             "units": "percent", "direction": "up",
             "source": "Census CPS (from EB)",
             "note": "High-school completion and college attainment both climbed steeply — bachelor's up from 11% to ~39%.",
             "series": [
                 {"name": "High school or higher", "curated": "edu_hs"},
                 {"name": "Bachelor's or higher", "curated": "edu_ba"}]},
        ],
    },
    {
        "id": "vs-europe", "title": "America vs. Europe",
        "blurb": "Americans are materially richer than western Europeans — GDP and consumption "
                 "per person run well above the Big-4 — and buy far more space, cars and "
                 "appliances. But Europeans work much less and now live longer.",
        "charts": [
            {"id": "gdp-pc-compare", "title": "GDP per capita, US vs. Europe",
             "units": "constant 2015 dollars", "direction": "up",
             "source": "World Bank / OECD (via FRED, NY.GDP.PCAP.KD)",
             "note": "The US pulled ahead and stayed ahead: Germany ~65% of US, France ~59%, Italy ~51%.",
             "series": [
                 {"name": "United States", "live": "NYGDPPCAPKDUSA", "how": "annual", "start": "1960-01-01"},
                 {"name": "Germany", "live": "NYGDPPCAPKDDEU", "how": "annual", "start": "1960-01-01"},
                 {"name": "France", "live": "NYGDPPCAPKDFRA", "how": "annual", "start": "1960-01-01"},
                 {"name": "United Kingdom", "live": "NYGDPPCAPKDGBR", "how": "annual", "start": "1960-01-01"},
                 {"name": "Italy", "live": "NYGDPPCAPKDITA", "how": "annual", "start": "1960-01-01"},
                 {"name": "Euro area", "live": "NYGDPPCAPKDEMU", "how": "annual", "start": "1960-01-01"}]},
            {"id": "life-expectancy-compare", "title": "Life expectancy, US vs. Europe",
             "units": "years", "direction": "up",
             "source": "World Bank (via FRED, SP.DYN.LE00.IN)",
             "note": "The US started even with Europe in 1970 and has since fallen behind by 4–5 years.",
             "series": [
                 {"name": "United States", "live": "SPDYNLE00INUSA", "how": "annual", "start": "1960-01-01"},
                 {"name": "Germany", "live": "SPDYNLE00INDEU", "how": "annual", "start": "1960-01-01"},
                 {"name": "France", "live": "SPDYNLE00INFRA", "how": "annual", "start": "1960-01-01"},
                 {"name": "United Kingdom", "live": "SPDYNLE00INGBR", "how": "annual", "start": "1960-01-01"},
                 {"name": "Italy", "live": "SPDYNLE00INITA", "how": "annual", "start": "1960-01-01"}]},
        ],
    },
]


# ----------------------------------------------------------------------------- build
def resolve_series(spec, curated):
    if "special" in spec:
        if spec["special"] == "real_ahe":
            return real_ahe()
        raise KeyError(spec["special"])
    if "curated" in spec:
        return curated["series"][spec["curated"]]["data"]
    # live
    how = spec.get("how", "mean")
    start = spec.get("start", "1947-01-01")
    if how == "annual":
        return live(spec["live"], start=start, how="mean")  # already annual FRED series
    return live(spec["live"], start=start, how=how)


def build():
    curated = json.loads((OUT_DIR / "curated.json").read_text())
    sections, data_through = [], 0
    for sec in SECTIONS:
        charts = []
        for ch in sec["charts"]:
            out_series, asof = [], None
            for sp in ch["series"]:
                data = resolve_series(sp, curated)
                out_series.append({"name": sp["name"], "data": data})
                last_year = data[-1][0]
                data_through = max(data_through, last_year)
                # per-chart as-of: curated series carry an explicit asof; live = last year
                if "curated" in sp:
                    a = curated["series"].get(sp["curated"], {}).get("asof")
                    asof = a or asof
                else:
                    asof = str(last_year) if asof is None else max(asof, str(last_year))
            charts.append({
                "id": ch["id"], "title": ch["title"], "units": ch["units"],
                "direction": ch["direction"], "source": ch["source"],
                "note": ch["note"], "asof": asof, "series": out_series})
            print(f"  [{sec['id']}] {ch['id']}: "
                  f"{sum(len(s['data']) for s in out_series)} points across "
                  f"{len(out_series)} series")
        sections.append({"id": sec["id"], "title": sec["title"],
                         "blurb": sec["blurb"], "charts": charts})

    out = {
        "meta": {
            "title": "How Americans Are Doing",
            "data_through": data_through,
            "live_source": "FRED",
            "note": "Live series (income, consumption, GDP, inequality, cross-country) "
                    "are pulled from FRED and refresh monthly. Curated series (housing "
                    "quality, health detail, environment, crime, education, tech) are "
                    "transcribed from Jason Furman's Empirical Briefings and update when "
                    "those are re-run. See curated_SOURCES.md.",
        },
        "summary": curated["summary"],
        "sections": sections,
        "europe_compare": curated["europe_compare"],
    }
    (OUT_DIR / "data.json").write_text(json.dumps(out, separators=(",", ":")) + "\n")
    print(f"\nWrote data.json — {len(sections)} sections, "
          f"{sum(len(s['charts']) for s in sections)} charts, data through {data_through}.")


if __name__ == "__main__":
    build()
