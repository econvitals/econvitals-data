#!/usr/bin/env python3
"""The decision-feed reader's guards, on saved copies of every bank's feed (fixtures/feeds/, read
2026-10-25): each bank's title pattern picks its real decision item and nothing else on that feed;
every feed's date format parses; the fallback listings (ECB, RBA, BoJ, RBI) and the PBoC listing
page yield the decision; the statement excerpt is non-empty for every bank, including the BoJ's
PDF and the RBNZ's walled page (feed summary); a 404 or a timeout is an `error`, never an
exception, and `all_decisions` survives a fetcher that always fails.

Standard library only, no network:  python3 builders/central-bank-stance/test_feeds.py
"""
import socket
import sys
import unittest
import urllib.error
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import feeds  # noqa: E402

TODAY = date(2026, 10, 25)
FIX = Path(__file__).resolve().parent / "fixtures" / "feeds"
OLD = FIX.parent                      # the RBI pages fast_sources' tests already keep


def _read(name: str) -> str:
    return (FIX / name).read_text(encoding="utf-8", errors="replace")


# the statement each decision item links to, by a distinctive piece of its URL
STATEMENTS = {"monetary20260916a": "US", "ecb.mp260910": "XM", "monetary-policy-summary-and-minutes/2026/september-2026": "GB",
              "fad-press-release-2026-09-02": "CA", "mr-26-27.html": "AU", "policy-rate-unchanged-at-16.75": "SE",
              "pre_20260924": "CH", "2026-09-24-rate": "NO", "2026102014240299288": "CN"}

EXPECTED = {   # announced date, title — what the banks actually published, as of 2026-10-25
    "US": ("2026-09-16", "Federal Reserve issues FOMC statement"),
    "XM": ("2026-09-10", "Monetary policy decisions"),
    "GB": ("2026-09-17", "Bank rate maintained at 3.75% - September 2026 Monetary Policy Summary and Minutes"),
    "CA": ("2026-09-02", "Bank of Canada maintains the policy rate at 2¼%"),
    "AU": ("2026-09-29", "Statement by the Monetary Policy Board: Monetary Policy Decision"),
    "SE": ("2026-09-24", "Policy rate unchanged at 1.75 per cent"),
    "NZ": ("2026-09-02", "OCR increased by 25 basis points to 2.75%"),
    "JP": ("2026-09-18", "Change in the Guideline for Money Market Operations"),
    "IN": ("2026-10-07", "Resolution of the Monetary Policy Committee (MPC) October 5 to 7, 2026"),
    "CH": ("2026-09-24", "2026-09-24 - Monetary policy assessment of 24 September 2026"),
    "NO": ("2026-09-24", "Policy rate raised to 4.50%"),
    "CN": ("2026-10-20", "Announcement on Loan Prime Rate (October 20, 2026)"),
}

# titles that sit on the same feeds and must NOT be taken for a decision
NOT_DECISIONS = {
    "US": ["Minutes of the Federal Open Market Committee, September 15-16, 2026",
           "Federal Reserve Board and Federal Open Market Committee release economic projections from the September 15-16 FOMC meeting",
           "Minutes of the Board's discount rate meetings on August 17, September 9, and September 16, 2026"],
    "XM": ["Meeting of 9-10 September 2026", "Christine Lagarde: IMFC Statement", "Monetary policy statement"],
    "GB": ["Monetary Policy Committee dates for 2027", "Financial Policy Committee Record – September 2026",
           "Transcript of the Governor's pooled broadcast interview given on 17 September 2026"],
    "CA": ["Bank of Canada publishes its 2027 schedule for policy interest rate announcements and other major publications",
           "Bank of Canada Board launches process to fill external Deputy Governor position"],
    "AU": ["Appointment of Chief Risk Officer", "Release of Financial Stability Review – October 2026"],
    "SE": ["Minutes of the monetary policy meeting on 23 September 2026",
           "The Riksbank’s Business Survey: “There was some concern before the summer"],
    "NZ": ["Updates to the October Monetary Policy Review and November Financial Stability Report",
           "New Monetary Policy Committee member appointed", "Reserve Bank maintains Loan-to-Value Ratio (LVR) settings"],
    "JP": ["Summary of Opinions at the Monetary Policy Meeting on September 17 and 18, 2026",
           "Minutes of the Monetary Policy Meeting on July 30 and 31, 2026",
           "(Reference) Change in the Guideline for Money Market Operations (September 2026 MPM)",
           "Conduct of Funds-Supplying Operations against Pooled Collateral"],
    "IN": ["Minutes of the Monetary Policy Committee Meeting, October 5 to 7, 2026", "Governor’s Statement: October 7, 2026",
           "RBI to conduct 4-day Variable Rate Repo (VRR) auction under LAF on October 26, 2026"],
    "CH": ["2026-10-22 - Monetary policy - Monetary policy assessment of September 2026: Summary of discussions",
           "2026-09-30 - Quarterly Bulletin 3/2026", "2026-10-15 - Petra Tschudin: Current economic developments and the SNB's monetary policy"],
    "NO": ["Foreign exchange transactions in October 2026", "New regulation on monetary policy", "Minor changes in growth prospects"],
    "CN": ["Old Version", "Statistical Releases"],
}


def fake_fetch(url: str):
    """Every page as saved on 2026-10-25; the RBNZ answers 403 to anything that is not a browser."""
    for code, cfg in feeds.FEEDS.items():
        if url == cfg["url"] and cfg["kind"] == "rss":
            return _read(f"{code}.xml")
    if url == feeds.FEEDS["CN"]["url"]:
        return _read("CN_listing.html")
    for code in ("XM", "AU", "JP"):
        if url == feeds.listing_urls(code, TODAY)[0]:
            return _read(f"{code}_listing.html")
    if url == feeds.RBI_LISTING:
        return (OLD / "rbi_listing_fy2026-27.html").read_text(errors="replace")
    if url.endswith("prid=63742"):
        return _read("IN_statement.html")          # the full ASP.NET page, body inside one <form>
    if url.endswith("k260918a.pdf"):
        return (FIX / "JP_statement.pdf").read_bytes()
    if "rbnz.govt.nz" in url:
        raise urllib.error.HTTPError(url, 403, "Forbidden", {}, None)
    for piece, code in STATEMENTS.items():
        if piece in url:
            return _read(f"{code}_statement.html")
    raise KeyError(url)


class Patterns(unittest.TestCase):
    def test_every_bank_matches_its_decision_and_nothing_else_on_its_feed(self):
        for code, (_, title) in EXPECTED.items():
            self.assertTrue(feeds.is_decision(code, title), f"{code}: {title!r} should match")
            for other in NOT_DECISIONS[code]:
                self.assertFalse(feeds.is_decision(code, other), f"{code}: {other!r} must not match")

    def test_feed_fixtures_have_exactly_the_decision_items(self):
        want = {"US": 4, "GB": 2, "CA": 3, "SE": 3, "NZ": 4, "CH": 7, "NO": 1, "XM": 0, "AU": 0, "JP": 0, "IN": 0}
        for code, n in want.items():
            items = feeds.parse_feed(_read(f"{code}.xml"))
            hits = [it["title"] for it in items if feeds.is_decision(code, it["title"])]
            self.assertEqual(len(hits), n, f"{code}: {hits}")
            self.assertTrue(all(it["url"].startswith("http") for it in items), code)

    def test_listing_pages_yield_the_decision(self):
        for code, fixture, base in (("XM", "XM_listing.html", feeds.listing_urls("XM", TODAY)[0]),
                                    ("AU", "AU_listing.html", feeds.listing_urls("AU", TODAY)[0]),
                                    ("JP", "JP_listing.html", feeds.listing_urls("JP", TODAY)[0]),
                                    ("CN", "CN_listing.html", feeds.FEEDS["CN"]["url"])):
            rows = feeds.parse_listing(code, _read(fixture), base)
            self.assertTrue(rows, code)
            hit = feeds._pick(code, rows)
            self.assertEqual((hit["date"], hit["title"]), EXPECTED[code], code)
            self.assertTrue(hit["url"].startswith("https://"), hit["url"])
        rows = feeds.parse_listing("IN", (OLD / "rbi_listing_fy2026-27.html").read_text(errors="replace"), feeds.RBI_LISTING)
        self.assertEqual([r["date"] for r in rows], ["2026-10-07", "2026-08-05", "2026-06-05", "2026-04-08"])
        self.assertTrue(rows[0]["url"].endswith("prid=63742"))

    def test_boj_listing_skips_the_reference_note_and_strips_the_pdf_tag(self):
        rows = feeds.parse_listing("JP", _read("JP_listing.html"), feeds.listing_urls("JP", TODAY)[0])
        sept = [r for r in rows if r["date"] == "2026-09-18"]
        self.assertGreaterEqual(len(sept), 2)
        hits = [r for r in sept if feeds.is_decision("JP", r["title"])]
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0]["title"], "Change in the Guideline for Money Market Operations")
        self.assertTrue(hits[0]["url"].endswith("/mpr_2026/k260918a.pdf"))

    def test_listing_urls_try_this_year_then_last(self):
        self.assertEqual(feeds.listing_urls("AU", date(2027, 1, 3)),
                         ["https://www.rba.gov.au/media-releases/2027/", "https://www.rba.gov.au/media-releases/2026/"])
        self.assertEqual(feeds.listing_urls("IN", TODAY), [feeds.RBI_LISTING])
        self.assertEqual(feeds.listing_urls("GB", TODAY), [])


class Dates(unittest.TestCase):
    def test_every_format_the_feeds_use(self):
        cases = {
            "<![CDATA[Tue, 13 Oct 2026 18:00:00 GMT]]>": "2026-10-13",   # Fed
            "Sat, 24 Oct 2026 08:00:00 +0200": "2026-10-24",              # ECB
            "Fri, 23 Oct 2026 19:25:00": "2026-10-23",                    # RBI, no zone
            "2026-09-10T11:00:09+00:00": "2026-09-10",                    # Bank of Canada (dc:date)
            "2026-10-14T14:00:00+11:00": "2026-10-14",                    # RBA
            "2026-09-24T07:30:00Z": "2026-09-24",                         # SNB
            "2026-09-29T14:30+10:00": "2026-09-29",                       # RBA listing <time>
            "Wed, 02 Sep 2026 00:00:00 +1200": "2026-09-02",              # RBNZ
            "Thu, 24 Sep 2026 08:00:00 GMT": "2026-09-24",                # Norges Bank
            "29&nbsp;September 2026": "2026-09-29",                       # RBA listing text
            "Sept.&nbsp;18,&nbsp;2026": "2026-09-18",                     # BoJ listing
            "October 20, 2026": "2026-10-20",                             # PBoC title
            "Oct 07, 2026": "2026-10-07",                                 # RBI listing
        }
        for raw, iso in cases.items():
            self.assertEqual(feeds.parse_date(raw), iso, raw)

    def test_junk_is_none(self):
        for raw in ("", None, "yesterday", "Foo 99, 2026", "2026-13-40"):
            self.assertIsNone(feeds.parse_date(raw), raw)

    def test_feed_item_dates_all_parse(self):
        for code in ("US", "XM", "GB", "CA", "AU", "SE", "NZ", "JP", "IN", "CH", "NO"):
            items = feeds.parse_feed(_read(f"{code}.xml"))
            self.assertTrue(items, code)
            self.assertTrue(all(it["date"] for it in items), f"{code}: an undated item")


class Decisions(unittest.TestCase):
    def setUp(self):
        self.res = feeds.all_decisions(fake_fetch, TODAY)

    def test_every_bank_found_its_decision(self):
        for code, (announced, title) in EXPECTED.items():
            r = self.res[code]
            self.assertIsNone(r["error"], f"{code}: {r['error']}")
            self.assertEqual((r["announced"], r["title"]), (announced, title), code)
            self.assertTrue(r["url"].startswith("http"), code)
            self.assertGreater(r["items_seen"], 0, code)
            self.assertTrue(r["fetched_at"], code)

    def test_excerpt_is_non_empty_and_is_the_statement(self):
        openers = {
            "US": "The Federal Open Market Committee approved", "XM": "The Governing Council today decided",
            "GB": "Monetary Policy Summary, September 2026", "CA": "The Bank of Canada today held",
            "AU": "At its meeting today, the Board decided", "SE": "The Executive Board has decided",
            "JP": "September 18, 2026", "IN": "Monetary Policy Decisions",
            "CH": "Swiss National Bank leaves SNB policy rate unchanged at 0%", "NO": "At its meeting on 23 September",
            "CN": "Under the authorization of the People’s Bank of China",
        }
        for code, opener in openers.items():
            r = self.res[code]
            self.assertTrue(r["excerpt"], code)
            self.assertTrue(r["excerpt"].startswith(opener), f"{code}: {r['excerpt'][:120]!r}")
            self.assertLessEqual(len(r["excerpt"]), feeds.EXCERPT_CHARS)
            self.assertLessEqual(len(r["text"]), feeds.TEXT_CHARS)
            self.assertTrue(r["text"].startswith(r["excerpt"]), code)
            self.assertNotIn("<", r["text"], f"{code}: a tag survived")

    def test_boj_pdf_reads_as_prose(self):
        t = self.res["JP"]["text"]
        self.assertIn("the Policy Board of the Bank of Japan decided", t)
        self.assertIn("uncollateralized overnight call rate to remain at around 1.25", t)
        self.assertIn("effective from September 24, 2026", t)

    def test_rbnz_walled_page_falls_back_to_the_feed_summary(self):
        r = self.res["NZ"]
        self.assertIsNone(r["error"])
        self.assertIn("403", r["note"])
        self.assertTrue(r["excerpt"].startswith("Monetary Policy Statement media release"))

    def test_short_feeds_say_they_read_the_listing(self):
        for code in ("XM", "AU", "JP", "IN"):
            self.assertIn("listing page", self.res[code]["note"], code)
            self.assertNotEqual(self.res[code]["source_url"], feeds.FEEDS[code]["url"], code)
        for code in ("US", "GB", "CA", "SE", "CH", "NO", "CN"):
            self.assertIsNone(self.res[code]["note"], code)

    def test_fed_statement_is_whole_and_free_of_chrome(self):
        t = self.res["US"]["text"]
        self.assertIn("12 – 0 vote", t)
        self.assertIn("Implementation Note issued September 16, 2026", t)
        self.assertNotIn("Share", t)
        self.assertNotIn("Skip to main content", t)


class Failures(unittest.TestCase):
    def test_404_is_an_error_not_an_exception(self):
        def four_oh_four(url):
            raise urllib.error.HTTPError(url, 404, "Not Found", {}, None)
        r = feeds.latest_decision("US", four_oh_four)
        self.assertIn("404", r["error"])
        self.assertIsNone(r["announced"])
        self.assertEqual(r["items_seen"], 0)
        self.assertEqual(r["code"], "US")

    def test_timeout_is_an_error_not_an_exception(self):
        def slow(url):
            raise socket.timeout("timed out")
        r = feeds.latest_decision("XM", slow)
        self.assertIn("timed out", r["error"])
        r = feeds.latest_decision("CN", lambda url: (_ for _ in ()).throw(urllib.error.URLError("timed out")))
        self.assertIn("timed out", r["error"])

    def test_a_feed_with_no_decision_and_no_listing_is_an_error(self):
        r = feeds.latest_decision("GB", lambda url: _read("US.xml"))     # a feed, but not a BoE one
        self.assertIsNone(r["announced"])
        self.assertIn("no decision item among 15 items", r["error"])

    def test_garbage_is_an_error(self):
        r = feeds.latest_decision("CA", lambda url: "<html>not a feed</html>")
        self.assertIn("no decision item among 0 items", r["error"])
        r = feeds.latest_decision("ZZ", fake_fetch)
        self.assertIn("no feed configured", r["error"])

    def test_all_decisions_never_raises(self):
        def broken(url):
            raise RuntimeError("boom")
        res = feeds.all_decisions(broken, TODAY)
        self.assertEqual(set(res), set(feeds.FEEDS))
        self.assertTrue(all(r["error"] and "boom" in r["error"] for r in res.values()))

    def test_statement_failure_keeps_the_decision(self):
        def feed_only(url):
            if url == feeds.FEEDS["SE"]["url"]:
                return _read("SE.xml")
            raise urllib.error.HTTPError(url, 500, "Server Error", {}, None)
        r = feeds.latest_decision("SE", feed_only)
        self.assertIsNone(r["error"])
        self.assertEqual(r["announced"], "2026-09-24")
        self.assertIn("500", r["note"])
        self.assertTrue(r["excerpt"])          # the Riksbank feed carries a summary


class Pdf(unittest.TestCase):
    def test_statement_text_accepts_pdf_bytes(self):
        t = feeds.statement_text((FIX / "JP_statement.pdf").read_bytes(), "JP")
        self.assertTrue(t.startswith("September 18, 2026"))
        self.assertIn("Change in the Guideline for Money Market Operations", t)

    def test_html_text_strips_scripts_and_blocks(self):
        page = ("<html><head><title>x</title><script>var a=1;</script></head><body><nav>menu</nav>"
                "<main><h1>Title</h1><p class='a'>One &amp; two.</p><p>Three</p></main><footer>f</footer></body></html>")
        self.assertEqual(feeds.html_text(page), "Title\nOne & two.\nThree")


if __name__ == "__main__":
    unittest.main(verbosity=1)
