#!/usr/bin/env python3
"""The 75-day rule for hand-entered rates (build.py: stale_manual_entries / check_stale).

Standard library only:  python3 builders/central-bank-stance/test_build.py
"""
import io
import sys
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import build  # noqa: E402

TODAY = date(2026, 10, 21)


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

    def test_the_shipped_config_is_fresh_on_its_own_verified_day(self):
        import yaml
        c = yaml.safe_load(build.CONFIG.read_text())
        hands = [s for s in c["banks"] if s.get("manual_rate")]
        self.assertTrue(hands, "the config should carry at least one hand entry while BIS is stopped")
        for s in hands:
            v = s["manual_rate"].get("verified") or s["manual_rate"]["date"]
            v = v if isinstance(v, date) else date.fromisoformat(str(v))
            self.assertEqual(build.stale_manual_entries(c, v), [])
            self.assertEqual(len(build.stale_manual_entries(c, v + timedelta(days=76))), len(hands))


if __name__ == "__main__":
    unittest.main(verbosity=1)
