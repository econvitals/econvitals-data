#!/usr/bin/env python3
"""markets.py's guards, on the real responses saved under fixtures/markets/ on 2026-10-25
(Fed relay, futures path, Polymarket's interest-rates and fomc listings, BoE OIS) and
2026-10-26 (Polymarket's Global-Rates listing, trimmed to the decision events; ten Kalshi
series read direct; the ASX RBA Rate Tracker): each parser on its fixture, bracket
probabilities summing to about 1, the nearest UPCOMING event chosen (today pinned), a dead
source reported as unavailable rather than raised, "no event listed" kept apart from "could
not be read", and the one no-free-source bank (China).

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
    mk.GAMMA_URL.format(tag="Global-Rates"): (FIX / "polymarket_global-rates_2026-10-26.json").read_bytes(),
    mk.ASX_RBA_URL: (FIX / "asx_rba_2026-10-26.json").read_bytes(),
}
for _code, _series in mk.KALSHI_SERIES.items():
    BODIES[mk.KALSHI_API_URL.format(series=_series)] = (FIX / f"kalshi_{_series}_2026-10-26.json").read_bytes()
KALSHI_BODY = {c: BODIES[mk.KALSHI_API_URL.format(series=s)] for c, s in mk.KALSHI_SERIES.items()}
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
        self.assertEqual(list(by_name(r)), ["Polymarket", "Kalshi"])
        self.assertIn("openpyxl", r["errors"]["Bank of England OIS curve"])
        self.assertIsNone(r["error"])


class Kalshi(unittest.TestCase):
    def test_bracket_names_map_to_moves(self):
        for label, moves in [("Maintain current rate", 0), ("Maintains rate", 0), ("Cut 1-25bps", -1),
                             ("Cut 25bps", -1), ("Cut more than 25bps", -2), ("Cut >25bps", -2),
                             ("Hike 1-25bps", 1), ("Hike 25bps", 1), ("Hike more than 25bps", 2),
                             ("Hike >25bps", 2), ("Hike 50bps", 2), ("Something else", None)]:
            self.assertEqual(mk.kalshi_moves(label), moves, label)

    def test_decision_date_is_the_close_not_the_subtitle(self):
        jp = mk.kalshi_source(KALSHI_BODY["JP"], "JP", TODAY)       # subtitle "Oct 29, 2026 meeting"
        self.assertEqual(jp["decision_date"], "2026-10-30")
        au = mk.kalshi_source(KALSHI_BODY["AU"], "AU", TODAY)       # subtitle "Nov 2, 2026 meeting"
        self.assertEqual(au["decision_date"], "2026-11-03")
        ca = mk.kalshi_source(KALSHI_BODY["CA"], "CA", TODAY)       # subtitle "In Oct 2026"
        self.assertEqual(ca["decision_date"], "2026-10-28")

    def test_fixture_numbers(self):
        jp = mk.kalshi_source(KALSHI_BODY["JP"], "JP", TODAY)
        self.assertAlmostEqual(jp["p_hold"], 0.965, places=3)      # bid 0.96 / ask 0.97
        self.assertLess(abs(jp["prob_sum"] - 1), mk.PROB_SUM_TOL)
        self.assertFalse(jp["thin"])
        ca = mk.kalshi_source(KALSHI_BODY["CA"], "CA", TODAY)
        self.assertGreater(ca["p_hike"], 0.8)
        self.assertEqual(ca["url"], "https://kalshi.com/markets/kxcbdecisioncanada")

    def test_a_wide_book_falls_back_to_the_last_trade_and_is_thin(self):
        no = mk.kalshi_source(KALSHI_BODY["NO"], "NO", TODAY)      # hold bid 0.81 / ask 0.90
        self.assertAlmostEqual(no["p_hold"], 0.83, places=3)       # the last trade
        self.assertTrue(no["thin"])

    def test_nearest_event_still_trading(self):
        self.assertEqual(mk.kalshi_source(KALSHI_BODY["JP"], "JP", date(2026, 11, 1))["decision_date"],
                         "2026-12-18")                             # the October one has closed
        with self.assertRaises(mk.NotListed):
            mk.kalshi_source(KALSHI_BODY["JP"], "JP", date(2027, 6, 1))
        with self.assertRaises(mk.NotListed):
            mk.kalshi_source(b'{"events": []}', "SE", TODAY)

    def test_no_event_listed_is_not_an_error(self):
        empty = mk.KALSHI_API_URL.format(series=mk.KALSHI_SERIES["SE"])
        r = mk.market_for("SE", fetch=lambda url: b'{"events": []}' if url == empty else fake_fetch(url),
                          today=TODAY)
        self.assertFalse(r["available"])
        self.assertEqual(r["errors"], {})
        self.assertIn("Kalshi", r["not_listed"])


class PolymarketGlobalRates(unittest.TestCase):
    EV = mk.parse_polymarket_events(BODIES[mk.GAMMA_URL.format(tag="Global-Rates")])

    def test_the_new_banks_and_their_dates(self):
        for code, dd in [("JP", "2026-10-30"), ("CA", "2026-10-28"), ("AU", "2026-11-03"),
                         ("NZ", "2026-10-28"), ("CH", "2026-12-10")]:
            src = mk.polymarket_source(self.EV, code, TODAY)
            self.assertEqual(src["decision_date"], dd, code)
            self.assertLess(abs(src["prob_sum"] - 1), mk.PROB_SUM_TOL, code)

    def test_no_event_for_sweden_or_norway(self):
        for code in ("SE", "NO"):
            self.assertNotIn(code, mk.POLYMARKET_TITLE)


class ASX(unittest.TestCase):
    BODY = BODIES[mk.ASX_RBA_URL]

    def test_fixture(self):
        s = mk.parse_asx_rba(self.BODY, date(2026, 10, 26))
        self.assertEqual((s["p_cut"], s["p_hold"], s["p_hike"]), (0, 0.38, 0.62))
        self.assertEqual(s["asof"], "2026-10-26")
        self.assertEqual(s["expected_rate"], 4.85)

    def test_stale_or_directionless_is_an_error(self):
        with self.assertRaises(ValueError):
            mk.parse_asx_rba(self.BODY, date(2026, 11, 20))
        with self.assertRaises(ValueError):
            mk.parse_asx_rba(b'{"Ftre_Cash_Rate_Change": 0, "days": [{"Stlmnt_Dt": "2026-10-26", '
                             b'"Prob_No_Change": 100, "Prob_Change": 0}]}', date(2026, 10, 26))

    def test_a_cut_is_a_cut(self):
        s = mk.parse_asx_rba(b'{"Ftre_Cash_Rate": 4.35, "Ftre_Cash_Rate_Change": -0.25, "days": '
                             b'[{"Stlmnt_Dt": "2026-10-26", "Prob_No_Change": 30, "Prob_Change": 70}]}',
                             date(2026, 10, 26))
        self.assertEqual((s["p_cut"], s["p_hike"]), (0.7, 0))


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

    def test_polymarket_and_kalshi_banks(self):
        for code in ("XM", "IN", "JP", "CA", "NZ", "CH"):
            r = mk.market_for(code, fetch=fake_fetch, today=TODAY)
            self.assertTrue(r["available"], code)
            self.assertEqual([s["source"] for s in r["sources"]], ["Polymarket", "Kalshi"], code)
            self.assertEqual(r["errors"], {}, code)

    def test_australia_leads_with_the_asx_futures(self):
        r = mk.market_for("AU", fetch=fake_fetch, today=TODAY)
        self.assertEqual([s["source"] for s in r["sources"]], ["ASX futures", "Polymarket", "Kalshi"])

    def test_sweden_and_norway_are_kalshi_only(self):
        for code in ("SE", "NO"):
            r = mk.market_for(code, fetch=fake_fetch, today=TODAY)
            self.assertEqual([s["source"] for s in r["sources"]], ["Kalshi"], code)

    def test_no_free_source_banks(self):
        self.assertEqual(mk.NO_FREE_SOURCE, ("CN",))
        for code in mk.NO_FREE_SOURCE:
            r = mk.market_for(code, fetch=down, today=TODAY)
            self.assertEqual(r, {"code": code, "available": False, "sources": [], "errors": {},
                                 "not_listed": {}, "error": "no free source"})
        self.assertEqual(mk.market_for("ZZ", fetch=down)["error"], "unknown bank code 'ZZ'")
        self.assertEqual(mk.market_for("jp", fetch=down)["code"], "JP")

    def test_everything_unreachable_is_unavailable_not_an_exception(self):
        for code in [c for c in mk.ALL_CODES if c not in mk.NO_FREE_SOURCE]:
            r = mk.market_for(code, fetch=down, today=TODAY)
            self.assertFalse(r["available"], code)
            self.assertEqual(r["sources"], [], code)
            self.assertIn("unreachable", r["error"], code)
            if code in mk.POLYMARKET_TITLE:
                self.assertIn("Polymarket", r["errors"], code)
            if code in mk.KALSHI_SERIES or code == "US":
                self.assertIn("Kalshi", r["errors"], code)

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
        # nobody listing an event is NOT a read failure: it is kept out of `errors`
        self.assertIn("Polymarket", r["not_listed"])
        self.assertNotIn("Polymarket", r["errors"])
        self.assertIn("Kalshi", r["errors"])                      # Kalshi really was down

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
        self.assertEqual([c for c in mk.ALL_CODES if not res[c]["available"]], ["CN"])
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
