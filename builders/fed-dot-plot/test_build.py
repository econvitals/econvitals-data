#!/usr/bin/env python3
"""The dot plot's scoring: dots are scored against the target-range midpoint in force at
year-end (Jason, 2026-10-23), not the following January's effective rate — and the realized
rate's two Board readers: the FOMC target off openmarket.htm (a saved copy,
fixtures/fomc_openmarket.htm, read 2026-11-15) and the H.15 effective rate off its XML package.

    python builders/fed-dot-plot/test_build.py        (plain asserts, no network)
"""
import datetime as dt
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import build  # noqa: E402


def daily(start, end, v):
    d, out = dt.date.fromisoformat(start), {}
    while d <= dt.date.fromisoformat(end):
        out[d.isoformat()] = v
        d += dt.timedelta(days=1)
    return out


def test_dec_2016_projection_for_end_2017_has_zero_error():
    # The FOMC raised the range to 1.25-1.50 on 2017-12-14; it was still in force on Dec 31.
    upper = {**daily("2017-06-15", "2017-12-13", 1.25), **daily("2017-12-14", "2018-03-21", 1.50)}
    lower = {**daily("2017-06-15", "2017-12-13", 1.00), **daily("2017-12-14", "2018-03-21", 1.25)}
    ye = build.year_end_targets(upper, lower, {}, dt.date(2026, 10, 23))
    assert ye["2017"] == 1.375, ye
    mtg = {"date": "2016-12-14", "year": 2016, "median": {"2017": 1.375}}
    build.attach_errors([mtg], ye)
    assert mtg["one_year_ahead"] == {"target_year": 2017, "projected_median": 1.375, "actual": 1.375, "error": 0.0}, mtg


def test_dec_2022_cases():
    # end-2022: 4.25-4.50 from 2022-12-15 (midpoint 4.375); the Dec-2021 median for 2022 was 0.875.
    # end-2023: 5.25-5.50 from 2023-07-27 (midpoint 5.375); the Dec-2022 median for 2023 was 5.125.
    upper = {**daily("2022-11-03", "2022-12-14", 4.00), **daily("2022-12-15", "2023-01-31", 4.50),
             **daily("2023-07-27", "2024-01-31", 5.50)}
    lower = {**daily("2022-11-03", "2022-12-14", 3.75), **daily("2022-12-15", "2023-01-31", 4.25),
             **daily("2023-07-27", "2024-01-31", 5.25)}
    ye = build.year_end_targets(upper, lower, {}, dt.date(2026, 10, 23))
    assert ye["2022"] == 4.375 and ye["2023"] == 5.375, ye
    m21 = {"date": "2021-12-15", "year": 2021, "median": {"2022": 0.875}}
    m22 = {"date": "2022-12-14", "year": 2022, "median": {"2023": 5.125}}
    build.attach_errors([m21, m22], ye)
    assert m21["one_year_ahead"]["error"] == 3.5, m21
    assert m22["one_year_ahead"]["error"] == 0.25, m22


def test_incomplete_year_is_not_scored():
    upper, lower = daily("2026-09-18", "2026-10-23", 4.00), daily("2026-09-18", "2026-10-23", 3.75)
    ye = build.year_end_targets(upper, lower, {}, dt.date(2026, 10, 23))
    assert "2026" not in ye, ye
    mtg = {"date": "2025-12-10", "year": 2025, "median": {"2026": 3.375}}
    build.attach_errors([mtg], ye)
    assert mtg["one_year_ahead"]["actual"] is None and mtg["one_year_ahead"]["error"] is None


def test_single_target_before_december_2008():
    single = daily("2007-10-31", "2008-01-21", 4.25)   # the single target from the Oct 31, 2007 cut
    ye = build.year_end_targets({}, {}, single, dt.date(2026, 10, 23))
    assert ye["2007"] == 4.25, ye
    # the range era starts 2008-12-16: end-2008 is the 0-0.25 range's midpoint, not a single target
    upper, lower = daily("2008-12-16", "2009-01-31", 0.25), daily("2008-12-16", "2009-01-31", 0.0)
    ye = build.year_end_targets(upper, lower, single, dt.date(2026, 10, 23))
    assert ye["2008"] == 0.125, ye


FOMC_PAGE = (Path(__file__).resolve().parent / "fixtures" / "fomc_openmarket.htm").read_text()


def test_board_target_page_gives_the_published_year_ends():
    rows = build.parse_fomc_target(FOMC_PAGE)
    assert len(rows) == 60 and rows[0] == ("2003-06-25", 1.0, 1.0), rows[:2]
    assert ("2008-04-30", 2.0, 2.0) in rows                # a <tr class="alt"> row
    assert ("2015-12-17", 0.25, 0.5) in rows               # dated by the effective day
    upper, lower, single = build.target_dailies(rows, "2026-11-15")
    assert min(single) == "2008-01-01" and max(single) == "2008-12-15", (min(single), max(single))
    assert min(upper) == build.RANGE_FROM and max(upper) == "2026-11-15"
    ye = build.year_end_targets(upper, lower, single, dt.date(2026, 11, 15))
    assert ye == {"2008": 0.125, "2009": 0.125, "2010": 0.125, "2011": 0.125, "2012": 0.125,
                  "2013": 0.125, "2014": 0.125, "2015": 0.375, "2016": 0.625, "2017": 1.375,
                  "2018": 2.375, "2019": 1.625, "2020": 0.125, "2021": 0.125, "2022": 4.375,
                  "2023": 5.375, "2024": 4.375, "2025": 3.625}, ye


def test_board_target_page_refuses_a_broken_table():
    i = FOMC_PAGE.find("October 30")
    start, end = FOMC_PAGE.rfind("<tr", 0, i), FOMC_PAGE.find("</tr>", i) + len("</tr>")
    for bad in (FOMC_PAGE[:start] + FOMC_PAGE[end:],                       # a dropped row
                FOMC_PAGE.replace("target federal funds rate or range", "x"),  # no heading
                "<html>Service unavailable</html>"):
        try:
            build.parse_fomc_target(bad)
        except ValueError:
            continue
        raise AssertionError("a broken target page was read")


def _h15(daily_n=1200):
    d0, obs = dt.date(2020, 1, 1), []
    for i in range(daily_n):
        d = (d0 + dt.timedelta(days=i)).isoformat()
        obs.append(f'<frb:Obs OBS_STATUS="A" OBS_VALUE="1.{i % 90:02d}" TIME_PERIOD="{d}" />')
    obs.append('<frb:Obs OBS_STATUS="ND" OBS_VALUE="-9999" TIME_PERIOD="2030-01-01" />')
    months = "".join(f'<frb:Obs OBS_STATUS="A" OBS_VALUE="2.00" TIME_PERIOD="{2000 + i // 12}-{i % 12 + 1:02d}-28" />'
                     for i in range(120))
    return ('<kf:Series FREQ="9" SERIES_NAME="RIFSPFF_N.B" UNIT="Percent">' + "".join(obs) + "</kf:Series>"
            '<kf:Series FREQ="129" SERIES_NAME="RIFSPFF_N.M" UNIT="Percent">' + months + "</kf:Series>")


def test_h15_no_data_sentinel_is_never_the_current_rate():
    daily, monthly = build.parse_h15_fedfunds(_h15())
    assert "2030-01-01" not in daily and min(daily.values()) > 0, max(daily)
    cur = build.current_rate(daily, monthly)
    assert cur["ym"] == max(daily) and cur["rate"] == daily[max(daily)], cur
    assert build.current_rate({}, monthly)["ym"] == "2009-12", build.current_rate({}, monthly)
    try:
        build.parse_h15_fedfunds(_h15(daily_n=10))           # not the release
    except ValueError:
        pass
    else:
        raise AssertionError("a short H.15 package was read")


def test_source_line_names_only_what_served():
    both = build.source_line({"target": "board", "current": "board"})
    assert "FRED" not in both and "openmarket.htm" in both and "H.15" in both, both
    assert "FRED DFEDTARU" in build.source_line({"target": "fred", "current": "board"})
    assert "FRED DFF" in build.source_line({"target": "board", "current": "fred"})


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print("PASS ", name)
    print("OK: the dots are scored against the year-end target midpoint.")
