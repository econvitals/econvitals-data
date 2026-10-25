#!/usr/bin/env python3
"""markets.py's guards, on the real responses saved under fixtures/markets/ on 2026-10-25:
each parser on its fixture, bracket probabilities summing to about 1, the nearest UPCOMING
event chosen (today pinned), a dead source reported as unavailable rather than raised, and
the no-free-source banks.

Standard library only, no network:  python3 builders/central-bank-stance/test_markets.py
The Bank of England OIS parse needs openpyxl and is skipped (not failed) without it; what
IS checked without it is that the missing module is reported, not raised.
"""
import sys
import unittest
import urllib.error
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import markets as mk  # noqa: E402

TODAY = date(2026, 10, 25)
FIX = Path(__file__).resolve().parent / "fixtures" / "markets"
BODIES = {
    mk.KALSHI_URL: (FIX / "kalshi_2026-10-25.json").read_bytes(),
    mk.FED_FUNDS_PATH_URL: (FIX / "fed-funds-path_2026-10-25.json").read_bytes(),
    mk.GAMMA_URL.format(tag="interest-rates"): (FIX / "polymarket_interest-rates_2026-10-25.json").read_bytes(),
    mk.GAMMA_URL.format(tag="fomc"): (FIX / "polymarket_fomc_2026-10-25.json").read_bytes(),
    mk.BOE_OIS_ZIP_URL: (FIX / "boe_latest-yield-curve-data_2026-10-25.zip").read_bytes(),
}
try:
    import openpyxl  # noqa: F401
    HAVE_OPENPYXL = True
except ImportError:
    HAVE_OPENPYXL = False


def fake_fetch(url: str) -> bytes:
    """Every source as it answered on 2026-10-25 — no network."""
    if url not in BODIES:
        raise KeyError(url)
    return BODIES[url]


def down(url: str) -> bytes:
    raise urllib.error.URLError("unreachable (test)")


def only(*urls):
    """A fetch that serves the named URLs from the fixtures and is down for the rest."""
    def f(url):
        if url in urls:
            return BODIES[url]
        raise urllib.error.URLError(f"down (test): {url}")
    return f


def by_name(res: dict) -> dict:
    return {s["source"]: s for s in res["sources"]}


EVENTS = (mk.parse_polymarket_events(BODIES[mk.GAMMA_URL.format(tag="interest-rates")])
          + mk.parse_polymarket_events(BODIES[mk.GAMMA_URL.format(tag="fomc")]))


class Kalshi(unittest.TestCase):
    def test_fixture(self):
        s = mk.parse_kalshi(BODIES[mk.KALSHI_URL])
        self.assertEqual(s["source"], "Kalshi")
        self.assertEqual(s["decision_date"], "2026-10-28")
        self.assertEqual(s["asof"], "2026-10-25T07:51:42.072Z")
        self.assertEqual((s["p_cut"], s["p_hold"], s["p_hike"]), (0.0196, 0.9118, 0.0686))
        self.assertEqual([o["moves"] for o in s["outcomes"]], [-2, -1, 0, 1, 2])
        self.assertAlmostEqual(sum(o["prob"] for o in s["outcomes"]), 1.0, delta=0.01)
        self.assertFalse(s["thin"])

    def test_relay_reporting_unavailable_is_an_error(self):
        with self.assertRaises(ValueError):
            mk.parse_kalshi(b'{"available": false, "error": "Kalshi 503"}')
        with self.assertRaises(ValueError):
            mk.parse_kalshi(b'{"available": true, "next_meeting": {}}')


class FedFundsPath(unittest.TestCase):
    def test_implied_path_from_this_month_on(self):
        s = mk.parse_fed_funds_path(BODIES[mk.FED_FUNDS_PATH_URL], TODAY)
        self.assertEqual(s["source"], "CME futures (chartbook)")
        self.assertEqual(s["implied_path"], [{"date": "2026-10", "rate": 3.875},
                                             {"date": "2026-11", "rate": 3.875},
                                             {"date": "2026-12", "rate": 4.125}])
        self.assertEqual((s["actual_now"], s["actual_through"]), (3.875, "2026-10"))
        self.assertIsNone(s["decision_date"])

    def test_past_months_are_dropped_and_an_empty_future_is_an_error(self):
        s = mk.parse_fed_funds_path(BODIES[mk.FED_FUNDS_PATH_URL], date(2026, 12, 1))
        self.assertEqual([p["date"] for p in s["implied_path"]], ["2026-12"])
        with self.assertRaises(ValueError):
            mk.parse_fed_funds_path(BODIES[mk.FED_FUNDS_PATH_URL], date(2027, 3, 1))
        with self.assertRaises(ValueError):
            mk.parse_fed_funds_path(b'{"columns": ["actual"], "data": []}', TODAY)


class PolymarketParses(unittest.TestCase):
    def test_bracket_names_map_to_moves(self):
        def o(name, yes="0.4"):
            return mk.polymarket_outcome({"groupItemTitle": name, "outcomes": '["Yes", "No"]',
                                          "outcomePrices": f'["{yes}", "0.6"]'})
        self.assertEqual(o("50+ bps decrease"), {"label": "Cut >25 bp", "prob": 0.4, "moves": -2})
        self.assertEqual(o("25 bps decrease")["moves"], -1)
        self.assertEqual(o("No change")["moves"], 0)
        self.assertEqual(o("No Change")["label"], "Hold")
        self.assertEqual(o("25 bps increase"), {"label": "Hike 25 bp", "prob": 0.4, "moves": 1})
        self.assertEqual(o("50+ bps increase")["moves"], 2)
        self.assertIsNone(o("What will Warsh say?"))

    def test_question_text_is_the_fallback_for_the_bracket_name(self):
        m = {"question": "Bank of England increases interest rates by 25 bps after November 2026 meeting?",
             "outcomes": ["Yes", "No"], "outcomePrices": ["0.969", "0.031"]}
        self.assertEqual(mk.polymarket_outcome(m), {"label": "Hike 25 bp", "prob": 0.969, "moves": 1})

    def test_decision_date_comes_from_the_description_last_day_named(self):
        self.assertEqual(mk.polymarket_decision_date(
            {"description": "... meeting scheduled for October 27-28, 2026 according to ...",
             "endDate": "2026-10-29T03:59:00Z"}), "2026-10-28")
        self.assertEqual(mk.polymarket_decision_date(
            {"description": "scheduled for December 16–17, 2026, as listed"}), "2026-12-17")
        self.assertEqual(mk.polymarket_decision_date(
            {"description": "with the decision to be announced on December 4, 2026, as"}), "2026-12-04")
        self.assertEqual(mk.polymarket_decision_date(
            {"description": "no date here", "endDate": "2026-11-05T23:59:00Z"}), "2026-11-05")
        self.assertIsNone(mk.polymarket_decision_date({"description": ""}))

    def test_fixture_events_per_bank(self):
        for code, title, dd in (("US", "Fed Decision in October?", "2026-10-28"),
                                ("XM", "ECB Interest Rates: October 2026", "2026-10-29"),
                                ("GB", "Bank of England decision in November?", "2026-11-05"),
                                ("IN", "Reserve Bank of India decision in December", "2026-12-04")):
            s = mk.polymarket_source(EVENTS, code, TODAY)
            self.assertEqual(s["event_title"], title, code)
            self.assertEqual(s["decision_date"], dd, code)
            self.assertEqual([o["moves"] for o in s["outcomes"]], [-2, -1, 0, 1, 2], code)
            self.assertAlmostEqual(sum(o["prob"] for o in s["outcomes"]), 1.0, delta=mk.PROB_SUM_TOL, msg=code)
            self.assertAlmostEqual(s["p_cut"] + s["p_hold"] + s["p_hike"], 1.0, delta=mk.PROB_SUM_TOL, msg=code)
            self.assertNotIn("note", s, code)
            self.assertTrue(s["url"].startswith("https://polymarket.com/event/"), code)

    def test_fixture_numbers(self):
        self.assertEqual(mk.polymarket_source(EVENTS, "US", TODAY)["p_hold"], 0.955)
        xm = mk.polymarket_source(EVENTS, "XM", TODAY)
        self.assertEqual((xm["p_cut"], xm["p_hold"], xm["p_hike"]), (0.001, 0.981, 0.0155))
        gb = mk.polymarket_source(EVENTS, "GB", TODAY)
        self.assertEqual((gb["p_hold"], gb["p_hike"]), (0.0265, 0.975))
        self.assertEqual(mk.polymarket_source(EVENTS, "IN", TODAY)["p_hike"], 0.85)

    def test_thin_flag(self):
        self.assertFalse(mk.polymarket_source(EVENTS, "XM", TODAY)["thin"])     # $61k / $657k
        self.assertFalse(mk.polymarket_source(EVENTS, "GB", TODAY)["thin"])     # $38k / $414k
        self.assertTrue(mk.polymarket_source(EVENTS, "IN", TODAY)["thin"])      # $11k / $18k
        self.assertTrue(mk.polymarket_source(EVENTS, "GB", date(2026, 11, 6))["thin"])  # Dec: $6k / $15k
        self.assertEqual((mk.THIN_LIQUIDITY_USD, mk.THIN_VOLUME_USD), (10_000, 25_000))

    def test_nearest_upcoming_event_not_a_past_one(self):
        # On 2026-10-25 the ECB's October event (decision 10-29) is ahead of December's.
        self.assertEqual(mk.polymarket_source(EVENTS, "XM", TODAY)["decision_date"], "2026-10-29")
        # On decision day it is still the next decision.
        self.assertEqual(mk.polymarket_source(EVENTS, "XM", date(2026, 10, 29))["decision_date"], "2026-10-29")
        # The day after, October is past: December is next even though its event is still listed.
        self.assertEqual(mk.polymarket_source(EVENTS, "XM", date(2026, 10, 30))["decision_date"], "2026-12-17")
        self.assertEqual(mk.polymarket_source(EVENTS, "GB", date(2026, 11, 6))["event_title"],
                         "Bank of England decision in December?")
        self.assertEqual(mk.polymarket_source(EVENTS, "US", date(2026, 10, 29))["event_title"],
                         "Fed Decision in December?")
        # Nothing ahead: an error, which market_for reports as unavailable.
        with self.assertRaises(ValueError):
            mk.polymarket_source(EVENTS, "IN", date(2026, 12, 5))

    def test_out_of_line_book_carries_a_note(self):
        ev = [{"title": "ECB Interest Rates: March 2027", "endDate": "2027-03-11T12:00:00Z",
               "description": "scheduled for March 10-11, 2027", "liquidity": 50000, "volume": 90000,
               "markets": [{"groupItemTitle": "No change", "outcomes": '["Yes","No"]', "outcomePrices": '["0.6","0.4"]'},
                           {"groupItemTitle": "25 bps increase", "outcomes": '["Yes","No"]', "outcomePrices": '["0.6","0.4"]'}]}]
        s = mk.polymarket_source(ev, "XM", TODAY)
        self.assertIn("sum to 1.200", s["note"])
        self.assertEqual(s["prob_sum"], 1.2)


@unittest.skipUnless(HAVE_OPENPYXL, "openpyxl not installed here (the Action installs it)")
class BankOfEnglandOIS(unittest.TestCase):
    def test_fixture(self):
        s = mk.parse_boe_ois(BODIES[mk.BOE_OIS_ZIP_URL])
        self.assertEqual(s["source"], "Bank of England OIS curve")
        self.assertEqual(s["curve_date"], "2026-10-22")
        self.assertEqual(s["asof"], s["curve_date"])
        self.assertEqual([p["months"] for p in s["implied_path"]], [3, 6, 12, 24])
        self.assertEqual([p["date"] for p in s["implied_path"]], ["2027-01", "2027-04", "2027-10", "2028-10"])
        for p in s["implied_path"]:
            self.assertTrue(0 < p["rate"] < 10, p)
        self.assertEqual([p["rate"] for p in s["implied_path"]], [4.131, 4.462, 4.85, 4.821])

    def test_month_arithmetic(self):
        self.assertEqual(mk._add_months(date(2026, 10, 22), 3), "2027-01")
        self.assertEqual(mk._add_months(date(2026, 12, 1), 24), "2028-12")
        self.assertEqual(mk._add_months(date(2026, 1, 31), 11), "2026-12")

    def test_a_zip_without_the_ois_workbook_is_an_error(self):
        import io
        import zipfile
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as z:
            z.writestr("GLC Nominal daily data current month.xlsx", b"not a workbook")
        with self.assertRaises(ValueError):
            mk.parse_boe_ois(buf.getvalue())


class MissingOpenpyxl(unittest.TestCase):
    """Without openpyxl the OIS source is reported, not raised, and the GB row still serves."""

    def setUp(self):
        self.saved = sys.modules.get("openpyxl")
        sys.modules["openpyxl"] = None  # makes `import openpyxl` raise ImportError

    def tearDown(self):
        if self.saved is None:
            sys.modules.pop("openpyxl", None)
        else:
            sys.modules["openpyxl"] = self.saved

    def test_reported_not_raised(self):
        with self.assertRaises(RuntimeError):
            mk.parse_boe_ois(BODIES[mk.BOE_OIS_ZIP_URL])
        r = mk.market_for("GB", fetch=fake_fetch, today=TODAY)
        self.assertTrue(r["available"])
        self.assertEqual(list(by_name(r)), ["Polymarket"])
        self.assertIn("openpyxl", r["errors"]["Bank of England OIS curve"])
        self.assertIsNone(r["error"])


class MarketFor(unittest.TestCase):
    def test_us_has_kalshi_futures_and_polymarket(self):
        r = mk.market_for("US", fetch=fake_fetch, today=TODAY)
        self.assertTrue(r["available"])
        names = [s["source"] for s in r["sources"]]
        self.assertEqual(names, ["Kalshi", "CME futures (chartbook)", "Polymarket"])
        self.assertEqual(r["errors"], {})
        k, p = by_name(r)["Kalshi"], by_name(r)["Polymarket"]
        self.assertEqual(k["decision_date"], p["decision_date"])        # both say 2026-10-28
        self.assertLess(abs(k["p_hold"] - p["p_hold"]), 0.1)            # 0.912 vs 0.955

    def test_gb_has_polymarket_and_the_ois_curve_when_openpyxl_is_here(self):
        r = mk.market_for("GB", fetch=fake_fetch, today=TODAY)
        self.assertTrue(r["available"])
        names = [s["source"] for s in r["sources"]]
        self.assertIn("Polymarket", names)
        if HAVE_OPENPYXL:
            self.assertIn("Bank of England OIS curve", names)
            self.assertEqual(r["errors"], {})

    def test_xm_and_in_are_polymarket_only(self):
        for code in ("XM", "IN"):
            r = mk.market_for(code, fetch=fake_fetch, today=TODAY)
            self.assertTrue(r["available"], code)
            self.assertEqual([s["source"] for s in r["sources"]], ["Polymarket"], code)

    def test_no_free_source_banks(self):
        for code in mk.NO_FREE_SOURCE:
            r = mk.market_for(code, fetch=down, today=TODAY)
            self.assertEqual(r, {"code": code, "available": False, "sources": [], "errors": {},
                                 "error": "no free source"})
        self.assertEqual(mk.market_for("ZZ", fetch=down)["error"], "unknown bank code 'ZZ'")
        self.assertEqual(mk.market_for("jp", fetch=down)["code"], "JP")

    def test_everything_unreachable_is_unavailable_not_an_exception(self):
        for code in ("US", "XM", "GB", "IN"):
            r = mk.market_for(code, fetch=down, today=TODAY)
            self.assertFalse(r["available"], code)
            self.assertEqual(r["sources"], [], code)
            self.assertIn("unreachable", r["error"], code)
            self.assertIn("Polymarket", r["errors"], code)
        self.assertIn("Kalshi", mk.market_for("US", fetch=down, today=TODAY)["errors"])

    def test_one_source_down_the_others_still_serve(self):
        r = mk.market_for("US", fetch=only(mk.FED_FUNDS_PATH_URL, mk.GAMMA_URL.format(tag="fomc")),
                          today=TODAY)
        self.assertTrue(r["available"])
        self.assertEqual([s["source"] for s in r["sources"]], ["CME futures (chartbook)", "Polymarket"])
        self.assertIn("Kalshi", r["errors"])
        self.assertIsNone(r["error"])

    def test_one_gamma_listing_down_the_other_still_serves(self):
        r = mk.market_for("XM", fetch=only(mk.GAMMA_URL.format(tag="interest-rates")), today=TODAY)
        self.assertTrue(r["available"])
        r = mk.market_for("XM", fetch=only(mk.GAMMA_URL.format(tag="fomc")), today=TODAY)
        self.assertFalse(r["available"])
        self.assertIn("no upcoming Polymarket decision event for XM", r["error"])

    def test_garbage_bodies_are_unavailable_not_an_exception(self):
        r = mk.market_for("US", fetch=lambda url: b"<html>503</html>", today=TODAY)
        self.assertFalse(r["available"])
        self.assertEqual(set(r["errors"]), {"Kalshi", "CME futures (chartbook)", "Polymarket"})
        r = mk.market_for("GB", fetch=lambda url: b"{}", today=TODAY)
        self.assertFalse(r["available"])

    def test_nothing_ahead_is_unavailable(self):
        r = mk.market_for("IN", fetch=fake_fetch, today=date(2027, 6, 1))
        self.assertFalse(r["available"])
        self.assertIn("no upcoming", r["error"])


class AllMarkets(unittest.TestCase):
    def test_every_bank_present_and_the_listings_fetched_once(self):
        calls = []

        def counting(url):
            calls.append(url)
            return fake_fetch(url)
        res = mk.all_markets(fetch=counting, today=TODAY)
        self.assertEqual(sorted(res), sorted(mk.ALL_CODES))
        self.assertEqual([c for c in mk.ALL_CODES if res[c]["available"]], ["US", "XM", "GB", "IN"])
        for c in mk.NO_FREE_SOURCE:
            self.assertEqual(res[c]["error"], "no free source")
        self.assertEqual(len(calls), len(set(calls)), "a URL was fetched twice")
        self.assertEqual(calls.count(mk.GAMMA_URL.format(tag="interest-rates")), 1)

    def test_never_raises(self):
        def explode(url):
            raise RuntimeError("boom")
        res = mk.all_markets(fetch=explode, today=TODAY)
        self.assertEqual(sorted(res), sorted(mk.ALL_CODES))
        self.assertFalse(any(r["available"] for r in res.values()))
        self.assertIn("boom", res["US"]["error"])


if __name__ == "__main__":
    unittest.main(verbosity=1)
