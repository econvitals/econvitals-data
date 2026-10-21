#!/usr/bin/env python3
"""The stance-matrix builder's guards: the RBI fast source's parses (on saved copies of the
RBI's pages, fixtures/), the India fallback order (RBI -> BIS -> manual_rate), out-of-range
rejection, and the 75-day rule for hand-entered rates (stale_manual_entries / check_stale).

Standard library only, no network:  python3 builders/central-bank-stance/test_build.py
"""
import io
import sys
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import build  # noqa: E402
import fast_sources as fs  # noqa: E402

TODAY = date(2026, 10, 21)
FIX = Path(__file__).resolve().parent / "fixtures"
HOME = (FIX / "rbi_home.html").read_text()
LISTING = (FIX / "rbi_listing_fy2026-27.html").read_text()
LISTING_PREV = (FIX / "rbi_listing_fy2025-26.html").read_text()
RES = {d: (FIX / f"rbi_resolution_{d}.html").read_text()
       for d in ("2026-10-07", "2026-08-05", "2026-06-05", "2026-04-08", "2026-02-06", "2025-12-05")}
PRID = {"63742": "2026-10-07", "63287": "2026-08-05", "62863": "2026-06-05", "62514": "2026-04-08",
        "62169": "2026-02-06", "61749": "2025-12-05"}


def fake_fetch(url: str) -> str:
    """The RBI's pages as saved on 2026-10-21 — no network."""
    if url == fs.RBI_HOME:
        return HOME
    if url == fs.RBI_LISTING:
        return LISTING
    for prid, d in PRID.items():
        if url.endswith("prid=" + prid):
            return RES[d]
    raise KeyError(url)


def fake_post(url: str, fields: dict) -> str:
    assert url == fs.RBI_LISTING and fields.get("hdnYear") == "2026" and "__VIEWSTATE" in fields
    return LISTING_PREV


class RBIParses(unittest.TestCase):
    def test_home_page_rate(self):
        self.assertEqual(fs.parse_rbi_home(HOME), 5.50)

    def test_home_page_out_of_range_is_rejected(self):
        with self.assertRaises(ValueError):
            fs.parse_rbi_home(HOME.replace("5.50%", "25.00%"))
        with self.assertRaises(ValueError):
            fs.parse_rbi_home(HOME.replace("Policy Repo Rate", "Something Else"))

    def test_listing_resolutions_newest_first(self):
        items = fs.parse_rbi_listing(LISTING)
        self.assertEqual([d for d, _ in items], ["2026-10-07", "2026-08-05", "2026-06-05", "2026-04-08"])
        self.assertTrue(items[0][1].endswith("prid=63742"))
        self.assertEqual([d for d, _ in fs.parse_rbi_listing(LISTING_PREV)][:2], ["2026-02-06", "2025-12-05"])

    def test_listing_form_fields_for_the_previous_year_postback(self):
        fields, fy = fs.parse_rbi_listing_form(LISTING)
        self.assertEqual(fy, 2027)
        for k in ("__VIEWSTATE", "__EVENTVALIDATION", "hdnYear", "UsrFontCntr$btn"):
            self.assertIn(k, fields)

    def test_resolution_hike_hold_and_cut(self):
        self.assertEqual(fs.parse_rbi_resolution(RES["2026-10-07"]), ("2026-10-07", 5.50, 5.25))
        self.assertEqual(fs.parse_rbi_resolution(RES["2026-08-05"]), ("2026-08-05", 5.25, 5.25))
        self.assertEqual(fs.parse_rbi_resolution(RES["2026-04-08"]), ("2026-04-08", 5.25, 5.25))
        self.assertEqual(fs.parse_rbi_resolution(RES["2026-02-06"]), ("2026-02-06", 5.25, 5.25))
        self.assertEqual(fs.parse_rbi_resolution(RES["2025-12-05"]), ("2025-12-05", 5.25, 5.50))

    def test_resolution_out_of_range_is_rejected(self):
        with self.assertRaises(ValueError):
            fs.parse_rbi_resolution(RES["2026-10-07"].replace("5.50 per cent", "55.0 per cent"))

    def test_series_back_to_bis_last_date(self):
        s = fs.rbi_in("2026-07-23", fetch=fake_fetch, post=fake_post)
        today = date.today().isoformat()
        self.assertEqual(s, [("2026-06-04", 5.25), ("2026-06-05", 5.25), ("2026-08-05", 5.25),
                             ("2026-10-07", 5.50), (today, 5.50)])

    def test_series_walks_back_a_fiscal_year_when_start_is_earlier(self):
        s = fs.rbi_in("2026-01-01", fetch=fake_fetch, post=fake_post)
        self.assertEqual(s[:3], [("2025-12-04", 5.50), ("2025-12-05", 5.25), ("2026-02-06", 5.25)])
        self.assertEqual(s[-2], ("2026-10-07", 5.50))

    def test_home_page_must_agree_with_the_latest_resolution(self):
        def fetch(url):
            return HOME.replace("5.50%", "5.75%") if url == fs.RBI_HOME else fake_fetch(url)
        with self.assertRaises(ValueError):
            fs.rbi_in("2026-07-23", fetch=fetch, post=fake_post)


class IndiaFallbackOrder(unittest.TestCase):
    BIS = [("2026-07-22", 5.25), ("2026-07-23", 5.25)]

    def with_feed(self, fn):
        saved = fs.FAST["IN"]
        fs.FAST["IN"] = ("RBI (test)", fn)
        return saved

    def tearDown(self):
        fs.FAST["IN"] = ("Reserve Bank of India — Policy Repo Rate (rbi.org.in home page + MPC resolutions)", fs.rbi_in)

    def test_feed_serves_and_the_hand_entry_is_then_ignored(self):
        self.with_feed(lambda start: fs.rbi_in(start, fetch=fake_fetch, post=fake_post))
        with redirect_stderr(io.StringIO()):
            s, fast = build.apply_fast("IN", list(self.BIS))
        self.assertEqual(fast["label"], "RBI (test)")
        self.assertEqual(s[-2:], [("2026-10-07", 5.50), (date.today().isoformat(), 5.50)])
        self.assertEqual(build.last_move(s), {"date": "2026-10-07", "delta_bps": 25, "direction": "up"})
        with redirect_stderr(io.StringIO()):
            s2, manual = build.apply_manual("IN", india(verified=TODAY), s)
        self.assertIsNone(manual)
        self.assertEqual(s2, s)

    def test_feed_down_falls_back_to_bis_then_the_hand_entry(self):
        def down(start):
            raise ValueError("rbi.org.in unreachable")
        self.with_feed(down)
        with redirect_stderr(io.StringIO()):
            s, fast = build.apply_fast("IN", list(self.BIS))
        self.assertIsNone(fast)
        self.assertEqual(s, self.BIS)                       # BIS, untouched
        with redirect_stderr(io.StringIO()):
            s2, manual = build.apply_manual("IN", india(verified=TODAY), s)
        self.assertEqual(s2[-2:], [("2026-10-07", 5.50), ("2026-10-21", 5.50)])
        self.assertIn("entered by hand", manual["label"].lower())

    def test_out_of_range_feed_value_falls_back(self):
        def fetch(url):
            return HOME.replace("5.50%", "25.00%") if url == fs.RBI_HOME else fake_fetch(url)
        self.with_feed(lambda start: fs.rbi_in(start, fetch=fetch, post=fake_post))
        with redirect_stderr(io.StringIO()):
            s, fast = build.apply_fast("IN", list(self.BIS))
        self.assertIsNone(fast)
        self.assertEqual(s, self.BIS)

    def test_feed_that_disagrees_with_bis_on_its_last_date_is_not_spliced(self):
        self.with_feed(lambda start: [("2026-07-01", 5.75), ("2026-10-07", 5.50), ("2026-10-21", 5.50)])
        with redirect_stderr(io.StringIO()):
            s, fast = build.apply_fast("IN", list(self.BIS))
        self.assertIsNone(fast)
        self.assertEqual(s, self.BIS)

    def test_the_window_reaches_back_to_bis_last_date(self):
        seen = {}
        def feed(start):
            seen["start"] = start
            return [("2026-07-23", 5.25), ("2026-10-21", 5.25)]
        self.with_feed(feed)
        with redirect_stderr(io.StringIO()):
            build.apply_fast("IN", [("2025-01-02", 5.25), ("2025-01-03", 5.25)])
        self.assertEqual(seen["start"], "2025-01-03")


def cfg(*banks):
    return {"banks": list(banks)}


def india(**manual):
    """India with a hand entry decided 2026-10-07; pass verified=... to date the last check."""
    return {"code": "IN", "name": "Reserve Bank of India",
            "manual_rate": {"value": 5.50, "date": date(2026, 10, 7), **manual}}


class StaleRule(unittest.TestCase):
    def test_limit_is_75_days(self):
        self.assertEqual(build.STALE_AFTER_DAYS, 75)

    def test_verified_today_is_fresh(self):
        self.assertEqual(build.stale_manual_entries(cfg(india(verified=TODAY)), TODAY), [])

    def test_exactly_75_days_is_still_fresh(self):
        c = cfg(india(verified=TODAY - timedelta(days=75)))
        self.assertEqual(build.stale_manual_entries(c, TODAY), [])

    def test_76_days_is_stale_and_names_bank_and_date(self):
        c = cfg(india(verified=TODAY - timedelta(days=76)))
        stale = build.stale_manual_entries(c, TODAY)
        self.assertEqual(len(stale), 1)
        self.assertEqual(stale[0]["code"], "IN")
        self.assertEqual(stale[0]["name"], "Reserve Bank of India")
        self.assertEqual(stale[0]["verified"], (TODAY - timedelta(days=76)).isoformat())
        self.assertEqual(stale[0]["age_days"], 76)

    def test_verified_as_iso_string_is_accepted(self):
        c = cfg(india(verified=(TODAY - timedelta(days=100)).isoformat()))
        self.assertEqual(build.stale_manual_entries(c, TODAY)[0]["age_days"], 100)

    def test_missing_verified_falls_back_to_the_decision_date(self):
        c = cfg(india())  # date 2026-10-07, no verified
        self.assertEqual(build.manual_age_days(c["banks"][0], TODAY), 14)
        self.assertEqual(build.stale_manual_entries(c, date(2027, 1, 1)), [{
            "code": "IN", "name": "Reserve Bank of India", "verified": "2026-10-07", "age_days": 86}])

    def test_banks_without_a_hand_entry_are_ignored(self):
        c = cfg({"code": "US", "name": "Federal Reserve"}, india(verified=TODAY - timedelta(days=200)))
        self.assertEqual([e["code"] for e in build.stale_manual_entries(c, TODAY)], ["IN"])
        self.assertIsNone(build.manual_age_days({"code": "US", "name": "Federal Reserve"}, TODAY))

    def test_check_stale_exit_codes_and_message(self):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            rc_fresh = build.check_stale(cfg(india(verified=TODAY)), TODAY)
            rc_stale = build.check_stale(cfg(india(verified=TODAY - timedelta(days=90))), TODAY)
        self.assertEqual(rc_fresh, 0)
        self.assertEqual(rc_stale, 1)
        self.assertIn("hand entries fresh", out.getvalue())
        msg = err.getvalue()
        self.assertIn("STALE HAND ENTRY: Reserve Bank of India (IN)", msg)
        self.assertIn((TODAY - timedelta(days=90)).isoformat(), msg)
        self.assertIn("90 days ago", msg)

    def test_the_shipped_config_carries_no_hand_entry_and_india_has_a_fast_source(self):
        import yaml
        c = yaml.safe_load(build.CONFIG.read_text())
        self.assertEqual([s["code"] for s in c["banks"] if s.get("manual_rate")], [],
                         "manual_rate is the emergency fallback; India reads the RBI's own site since 2026-10-21")
        self.assertIn("IN", fs.FAST)
        self.assertTrue(fs.FAST["IN"][0].startswith("Reserve Bank of India"))
        with redirect_stdout(io.StringIO()):
            self.assertEqual(build.check_stale(c, TODAY), 0)


if __name__ == "__main__":
    unittest.main(verbosity=1)
