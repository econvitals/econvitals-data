#!/usr/bin/env python3
"""The diffusion index's rules, offline: the 3-month stance window on a hand-built series, the
index arithmetic, the denominator (a bank with no data in a month is left out), the euro-area
cut, the two fetch paths (wildcard, then per-area with a failure dropped) and the no-network
failure mode — plus a run over a trimmed real BIS CSV (fixtures/diffusion/, five banks,
2022-2024, the `detail=dataonly` layout).

Standard library only, no network:  python3 builders/central-bank-stance/test_diffusion.py
"""
import io
import json
import sys
import unittest
from contextlib import redirect_stderr
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import diffusion as d  # noqa: E402

FIX = Path(__file__).resolve().parent / "fixtures" / "diffusion"
REAL = (FIX / "bis_cbpol_dataonly_2022-2024.csv").read_text()


def csv_text(rows, with_source=False):
    """Rows of (code, date, value[, source]) -> a BIS-shaped CSV string."""
    head = "FREQ,REF_AREA,TIME_PERIOD,OBS_VALUE" + (",SOURCE_REF" if with_source else "")
    body = [f"D,{r[0]},{r[1]},{r[2]}" + (f",{r[3]}" if with_source else "") for r in rows]
    return "\n".join([head] + body) + "\n"


def flat(code, start_month, end_month, value):
    """One observation on the 1st of every month from start to end (inclusive), at `value`."""
    return [(code, f"{m}-01", value) for m in d.month_range(start_month, end_month)]


class HandBuiltSeries(unittest.TestCase):
    """Bank A: 1.00 through 2020-02, hiked to 1.25 on 2020-03-15, flat after.
    Bank B: 2.00 through 2020-03, cut to 1.75 on 2020-04-10, flat after.
    Bank C: no data until 2020-05, then flat at 3.00. All three run to 2020-12."""

    def setUp(self):
        rows = (flat("A", "2019-10", "2020-12", 1.00) + [("A", "2020-03-15", 1.25)]
                + flat("B", "2019-10", "2020-12", 2.00) + [("B", "2020-04-10", 1.75)]
                + flat("C", "2020-05", "2020-12", 3.00))
        # the flat helper repeats the pre-move level after the move; make the levels honest
        rows = [(c, dt, (1.25 if c == "A" and dt >= "2020-03-15" else
                         1.75 if c == "B" and dt >= "2020-04-10" else v)) for c, dt, v in rows]
        self.text = csv_text(rows)
        self.out = d.build_diffusion(fetch=lambda url: self.text, today=date(2020, 12, 31))
        self.k = {m: i for i, m in enumerate(self.out["months"])}

    def stance(self, code):
        series, _ = d.parse_csv(self.text)
        months = self.out["months"]
        return dict(zip(months, d.stance_by_month(series[code], months)))

    def test_a_hike_counts_for_three_months_then_hold(self):
        a = self.stance("A")
        self.assertEqual(a["2020-02"], "hold")
        self.assertEqual([a[m] for m in ("2020-03", "2020-04", "2020-05")], ["hiking"] * 3)
        self.assertEqual(a["2020-06"], "hold")
        self.assertEqual(a["2020-12"], "hold")

    def test_a_cut_counts_for_three_months_then_hold(self):
        b = self.stance("B")
        self.assertEqual(b["2020-03"], "hold")
        self.assertEqual([b[m] for m in ("2020-04", "2020-05", "2020-06")], ["cutting"] * 3)
        self.assertEqual(b["2020-07"], "hold")

    def test_a_bank_with_no_data_is_left_out_of_n(self):
        c = self.stance("C")
        self.assertIsNone(c["2020-04"])
        self.assertEqual(c["2020-05"], "hold")
        self.assertEqual(self.out["n"][self.k["2020-04"]], 2)
        self.assertEqual(self.out["n"][self.k["2020-05"]], 3)
        self.assertEqual(self.out["months"][0], "2000-01")
        self.assertEqual(self.out["months"][-1], "2020-12")
        self.assertEqual(self.out["n"][self.k["2019-06"]], 0)          # nobody has data yet
        self.assertIsNone(self.out["index"][self.k["2019-06"]])
        self.assertEqual(self.out["n"][self.k["2019-10"]], 2)

    def test_index_arithmetic(self):
        o, k = self.out, self.k
        # 2020-03: A hiking, B hold          -> (1-0)/2 = 50%
        self.assertEqual((o["hiking"][k["2020-03"]], o["cutting"][k["2020-03"]], o["hold"][k["2020-03"]]), (1, 0, 1))
        self.assertEqual(o["index"][k["2020-03"]], 50.0)
        # 2020-04: A hiking, B cutting       -> 0%
        self.assertEqual(o["index"][k["2020-04"]], 0.0)
        # 2020-05: A hiking, B cutting, C hold -> 0%
        self.assertEqual((o["hiking"][k["2020-05"]], o["cutting"][k["2020-05"]], o["hold"][k["2020-05"]]), (1, 1, 1))
        self.assertEqual(o["index"][k["2020-05"]], 0.0)
        # 2020-06: A hold, B cutting, C hold -> -33.3%
        self.assertEqual(o["index"][k["2020-06"]], -33.3)
        # 2020-07 on: everyone on hold       -> 0%
        self.assertEqual(o["index"][k["2020-07"]], 0.0)
        for key in ("index", "hiking", "cutting", "hold", "n"):
            self.assertEqual(len(o[key]), len(o["months"]), key)

    def test_latest_lists_and_meta(self):
        lt = self.out["latest"]
        self.assertEqual(lt["month"], "2020-12")
        self.assertEqual([e["code"] for e in lt["hold"]], ["A", "B", "C"])
        self.assertEqual(lt["hiking"], [])
        self.assertEqual(lt["cutting"], [])
        m = self.out["meta"]
        self.assertEqual((m["n_banks"], m["n_series"], m["window_months"]), (3, 3, 3))
        self.assertEqual(m["fetch_mode"], "wildcard")
        self.assertEqual(m["dropped"], [])
        self.assertIsNone(m["error"])
        self.assertEqual(m["source"], "BIS — Central bank policy rates (WS_CBPOL)")
        json.dumps(self.out)  # serializable, nothing exotic in it

    def test_a_series_gone_dark_leaves_the_denominator(self):
        rows = flat("A", "2019-10", "2020-12", 1.0) + flat("B", "2019-10", "2020-06", 2.0)
        out = d.build_diffusion(fetch=lambda url: csv_text(rows), today=date(2020, 12, 31))
        k = {m: i for i, m in enumerate(out["months"])}
        self.assertEqual(out["n"][k["2020-06"]], 2)
        self.assertEqual(out["n"][k["2020-08"]], 2)   # B's last obs (June) is still inside the window
        self.assertEqual(out["n"][k["2020-09"]], 1)   # three months dark: out
        nc = out["meta"]["not_counted_latest"]
        self.assertEqual([(e["code"], e["last"]) for e in nc], [("B", "2020-06-01")])
        self.assertIn("no BIS observation", nc[0]["why"])


class ParsingAndEuro(unittest.TestCase):
    def test_nan_and_blank_rows_are_skipped(self):
        text = "FREQ,REF_AREA,TIME_PERIOD,OBS_VALUE\nD,US,2024-01-01,5.375\nD,US,2024-01-06,NaN\nD,US,2024-01-07,\nD,US,2024-01-08,5.375\n"
        series, names = d.parse_csv(text)
        self.assertEqual(series, {"US": [("2024-01-01", 5.375), ("2024-01-08", 5.375)]})
        self.assertEqual(names, {})

    def test_source_ref_column_supplies_the_name_when_present(self):
        text = csv_text([("US", "2024-01-01", 5.375, "US Federal Reserve System"),
                         ("ZZ", "2024-01-01", 1.0, "Bank of Nowhere")], with_source=True)
        series, names = d.parse_csv(text)
        self.assertEqual(names, {"US": "US Federal Reserve System", "ZZ": "Bank of Nowhere"})
        self.assertEqual(d.bank_name("ZZ", names), "Bank of Nowhere")
        self.assertEqual(d.bank_name("XM", {}), "European Central Bank")   # the built-in map
        self.assertEqual(d.bank_name("QQ", {}), "QQ")                      # unknown: the code

    def test_euro_members_are_cut_at_adoption_so_the_euro_area_counts_once(self):
        rows = (flat("HR", "2022-10", "2023-06", 2.5) + flat("XM", "2022-10", "2023-06", 2.5)
                + flat("GR", "2000-10", "2001-03", 4.75))
        series, _ = d.parse_csv(csv_text(rows))
        self.assertEqual(series["HR"][-1][0], "2022-12-01")   # Croatia: euro from 2023-01
        self.assertEqual(series["GR"][-1][0], "2000-12-01")   # Greece: euro from 2001-01
        self.assertEqual(len(series["XM"]), 9)                # the ECB is untouched
        out = d.build_diffusion(fetch=lambda url: csv_text(rows), today=date(2023, 6, 30))
        k = {m: i for i, m in enumerate(out["months"])}
        self.assertEqual(out["n"][k["2022-12"]], 2)
        self.assertEqual(out["n"][k["2023-03"]], 1)           # HR out once its window lapses
        why = {e["code"]: e["why"] for e in out["meta"]["not_counted_latest"]}
        self.assertEqual(why, {"HR": "euro area since 2023-01", "GR": "euro area since 2001-01"})

    def test_changes_and_month_helpers(self):
        self.assertEqual(d.changes([("a", 1.0), ("b", 1.0), ("c", 1.25), ("d", 1.0), ("e", 1.0)]),
                         [("c", 1), ("d", -1)])
        self.assertEqual(d.month_range("2019-11", "2020-02"), ["2019-11", "2019-12", "2020-01", "2020-02"])
        self.assertEqual(d.months_apart("2019-11", "2020-02"), 3)
        self.assertEqual(d.window_set("2020-01", 3), {"2019-11", "2019-12", "2020-01"})


class FetchPaths(unittest.TestCase):
    AVAIL = json.dumps({"data": {"dataConstraints": [{"cubeRegions": [{"keyValues": [
        {"id": "REF_AREA", "values": [{"value": "A"}, {"value": "B"}, {"value": "C"}]},
        {"id": "FREQ", "values": [{"value": "D"}]}]}]}]}})

    def test_wildcard_fails_then_per_area_with_one_bank_dropped(self):
        calls = []

        def fetch(url):
            calls.append(url)
            if url == d.WILDCARD_URL:
                raise OSError("HTTP 500")
            if url == d.AVAILABILITY_URL:
                return self.AVAIL
            if url.endswith("D.A?startPeriod=1999-10-01&format=csv&detail=dataonly"):
                return csv_text(flat("A", "2020-01", "2020-06", 1.0))
            if "/D.B?" in url:
                return csv_text(flat("B", "2020-01", "2020-06", 2.0))
            raise TimeoutError("C timed out")

        with redirect_stderr(io.StringIO()) as err:
            out = d.build_diffusion(fetch=fetch, today=date(2020, 6, 30))
        self.assertEqual(out["meta"]["fetch_mode"], "per-area")
        self.assertEqual(out["meta"]["dropped"], ["C"])
        self.assertEqual(out["meta"]["n_banks"], 2)
        self.assertIn("left out of this run's denominator: C", out["meta"]["note"])
        self.assertIn("wildcard fetch failed", err.getvalue())
        self.assertEqual(calls[0], d.WILDCARD_URL)
        self.assertEqual(calls[1], d.AVAILABILITY_URL)
        self.assertEqual(sorted(c for c in calls[2:]), sorted(d.AREA_URL.format(area=a) for a in "ABC"))

    def test_availability_failure_falls_back_to_the_known_area_list(self):
        def fetch(url):
            raise OSError("down")
        with redirect_stderr(io.StringIO()):
            self.assertEqual(d.discover_areas(fetch), d.KNOWN_AREAS)
            self.assertIn("US", d.KNOWN_AREAS)
            self.assertIn("XM", d.KNOWN_AREAS)

    def test_everything_down_never_raises(self):
        def fetch(url):
            raise OSError("down")
        with redirect_stderr(io.StringIO()):
            out = d.build_diffusion(fetch=fetch, today=date(2020, 6, 30))
        self.assertEqual(out["months"], [])
        self.assertEqual(out["index"], [])
        self.assertEqual(out["latest"], {"month": "2020-06", "hiking": [], "cutting": [], "hold": []})
        self.assertIn("wildcard and per-area calls both failed", out["meta"]["error"])
        self.assertEqual(out["meta"]["n_banks"], 0)

    def test_empty_wildcard_body_is_a_failure_not_an_empty_index(self):
        def fetch(url):
            if url == d.WILDCARD_URL:
                return "FREQ,REF_AREA,TIME_PERIOD,OBS_VALUE\n"
            if url == d.AVAILABILITY_URL:
                return self.AVAIL
            return csv_text(flat(url.split("/D.")[1][0], "2020-01", "2020-06", 1.0))
        with redirect_stderr(io.StringIO()):
            out = d.build_diffusion(fetch=fetch, today=date(2020, 6, 30))
        self.assertEqual(out["meta"]["fetch_mode"], "per-area")
        self.assertEqual(out["meta"]["n_banks"], 3)


class RealFixture(unittest.TestCase):
    """Five banks (US, XM, GB, JP, BR), daily, 2022-01-01..2024-12-31, in the data-only layout."""

    @classmethod
    def setUpClass(cls):
        cls.out = d.build_diffusion(fetch=lambda url: REAL, today=date(2024, 12, 31))
        cls.k = {m: i for i, m in enumerate(cls.out["months"])}

    def test_shape(self):
        self.assertEqual(self.out["meta"]["n_series"], 5)
        self.assertEqual(self.out["months"][-1], "2024-12")
        self.assertTrue(all(n == 5 for n in self.out["n"][self.k["2022-01"]:]))
        self.assertTrue(all(n == 0 for n in self.out["n"][:self.k["2022-01"]]))

    def test_the_2022_hiking_cycle_and_the_2024_cuts(self):
        o, k = self.out, self.k
        # 2022-01: nobody has moved yet in the fixture window -> all hold
        self.assertEqual((o["hiking"][k["2022-01"]], o["index"][k["2022-01"]]), (0, 0.0))
        # the Fed's first hike was 2022-03-17 (BoE and Brazil already hiking) -> 3 of 5
        self.assertEqual((o["hiking"][k["2022-03"]], o["index"][k["2022-03"]]), (3, 60.0))
        # by 2022-09 the ECB had joined (first hike 2022-07-27): 4 hiking, only the BoJ on hold
        self.assertEqual((o["hiking"][k["2022-09"]], o["hold"][k["2022-09"]], o["index"][k["2022-09"]]), (4, 1, 80.0))
        # 2024-06: ECB cut (06-12) and Brazil still cutting; nobody hiking
        self.assertEqual((o["hiking"][k["2024-06"]], o["cutting"][k["2024-06"]], o["index"][k["2024-06"]]), (0, 2, -40.0))
        # 2024-12: Fed, ECB, BoE cutting; Brazil hiking; BoJ's 2024-08-01 hike has lapsed to hold
        self.assertEqual((o["hiking"][k["2024-12"]], o["cutting"][k["2024-12"]], o["hold"][k["2024-12"]]), (1, 3, 1))
        self.assertEqual(o["index"][k["2024-12"]], -40.0)

    def test_latest_groups_carry_code_and_readable_name(self):
        lt = self.out["latest"]
        self.assertEqual(lt["month"], "2024-12")
        self.assertEqual(lt["hiking"], [{"code": "BR", "name": "Central Bank of Brazil"}])
        self.assertEqual([e["code"] for e in lt["cutting"]], ["GB", "XM", "US"])   # sorted by name
        self.assertEqual(lt["hold"], [{"code": "JP", "name": "Bank of Japan"}])
        self.assertEqual(self.out["meta"]["not_counted_latest"], [])

    def test_boj_hike_window_on_real_dates(self):
        series, _ = d.parse_csv(REAL)
        months = self.out["months"]
        jp = dict(zip(months, d.stance_by_month(series["JP"], months)))
        # hikes on 2024-03-21 (to 0.1) and 2024-08-01 (to 0.25)
        self.assertEqual([jp[m] for m in ("2024-02", "2024-03", "2024-05", "2024-06", "2024-08", "2024-10", "2024-11")],
                         ["hold", "hiking", "hiking", "hold", "hiking", "hiking", "hold"])


if __name__ == "__main__":
    unittest.main(verbosity=1)
