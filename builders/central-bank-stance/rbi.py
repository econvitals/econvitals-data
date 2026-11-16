"""
macrolib.rbi — the Reserve Bank of India's policy repo rate, read off the RBI's own web pages.
THE ONE RBI PARSER in the estate (2026-11-15).

The RBI publishes no machine-readable series a script may read: its data files sit on
`rbidocs.rbi.org.in` behind an F5 bot wall, DBIE (`data.rbi.org.in`) is an encrypted session
gateway, and its "EBR API" is issued on application to one fixed IP address. Neither is ever
tried here, and nothing here works around a bot wall. What answers a plain user agent is
`www.rbi.org.in` itself, and that is all this module reads:

    the home page              "Policy Repo Rate : 5.50%" in the Current Rates box — today's value
    the Monetary Policy        `scripts/Annualpolicy.aspx`, one fiscal year a page — which press
    Statements listing         releases are MPC resolutions, and on what date
    the MPC resolutions        the rate each meeting decided and the size of the move

and the two are the cross-check on each other: the home page must agree with the latest
resolution or the whole read is refused (`rbi_in` raises).

Jason's ruling of 2026-10-21 put this reader in the Central Bank Stance Matrix builder
(econvitals-data `builders/central-bank-stance/fast_sources.py`); his ruling of 2026-11-15
restored India to the chartbook's policy-rate charts, and the 2026-10-21 entry said to port the
reader rather than write a second one. So it moved HERE, and both readers of India's rate use it:

  * `macrolib.india.series("POLICY_RATE")` — `india:POLICY_RATE`, the end-of-month repo rate:
    the BIS relay's monthly history, then the months after it from these MPC decisions;
  * the Stance Matrix, whose builder imports this module.

THE STANCE MATRIX RUNS IN A PUBLIC REPO'S GITHUB ACTION THAT HAS NO MACROLIB (macrolib is
private and the workflow installs only pyyaml and openpyxl). It therefore carries a
BYTE-IDENTICAL COPY of this file, `builders/central-bank-stance/rbi.py`, and imports macrolib's
where macrolib is installed. `tests/test_rbi.py` here and `test_rbi_copy.py` there fail the
moment the two differ. Edit THIS file, then copy it over — never edit the copy. That is also why
this module is standard library only, with no import from the rest of macrolib.

Every parse is guarded: a missing sentence, an unreadable date or a rate outside 0-20 raises
ValueError, which callers treat as "the RBI's pages could not be read this run".
`fetch` and `post` are injectable, so the tests run on saved copies of the pages.

  python -m macrolib.rbi            # read the live pages, print the step series since 120 days ago
"""

from __future__ import annotations

import html
import re
import sys
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta

UA = {"User-Agent": "jfmacro-lab/1.0"}
TIMEOUT = 45

RBI_HOME = "https://www.rbi.org.in/"
RBI_LISTING = "https://www.rbi.org.in/scripts/Annualpolicy.aspx"
RBI_RATE_RANGE = (0.0, 20.0)   # a repo rate outside this is a parse gone wrong, not a policy
RBI_MAX_YEARS_BACK = 3         # fiscal-year listings to walk back for an anchor at/before `start`
_RBI_UP = ("increase", "raise", "hike")
_RBI_DOWN = ("reduce", "cut", "lower")


def _get(url: str) -> str:
    return urllib.request.urlopen(
        urllib.request.Request(url, headers=UA), timeout=TIMEOUT
    ).read().decode("utf-8", "replace")


def _post(url: str, fields: dict) -> str:
    data = urllib.parse.urlencode(fields).encode()
    req = urllib.request.Request(url, data=data, headers={
        **UA, "Content-Type": "application/x-www-form-urlencoded", "Referer": url})
    return urllib.request.urlopen(req, timeout=TIMEOUT).read().decode("utf-8", "replace")


def _clean(pairs) -> list[tuple[str, float]]:
    """Drop blanks/placeholders, coerce to float, sort chronologically."""
    out: list[tuple[str, float]] = []
    for d, v in pairs:
        if not d or v in (None, "", ".", "NaN", "ND"):
            continue
        try:
            out.append((d, float(v)))
        except (TypeError, ValueError):
            continue
    out.sort(key=lambda t: t[0])
    return out


def _text(html_: str) -> str:
    """Tags out, entities in, whitespace collapsed — the regexes below read prose."""
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", html_)))


def _rbi_rate(v) -> float:
    r = float(v)
    lo, hi = RBI_RATE_RANGE
    if not (lo < r < hi):
        raise ValueError(f"RBI repo rate {r} is outside {lo}-{hi}: a parse gone wrong")
    return r


def _rbi_date(s: str) -> str:
    return datetime.strptime(s.strip(), "%b %d, %Y").date().isoformat()


def parse_rbi_home(html_: str) -> float:
    """Today's policy repo rate from the home page's Current Rates box ('Policy Repo Rate : 5.50%')."""
    m = re.search(r"Policy Repo Rate\s*:\s*(\d+(?:\.\d+)?)\s*%", _text(html_))
    if not m:
        raise ValueError("RBI home page: no 'Policy Repo Rate : X%' text")
    return _rbi_rate(m.group(1))


def parse_rbi_listing(html_: str) -> list[tuple[str, str]]:
    """(iso date, press-release URL) of every 'Resolution of the Monetary Policy Committee' on a
    Monetary Policy Statements listing (one fiscal year), newest first."""
    out = []
    parts = re.split(r'<td class="tableheader"><b>([A-Z][a-z]{2} \d{2}, \d{4})</b></td>', html_)
    for i in range(1, len(parts), 2):
        m = re.search(r'Resolution of the Monetary Policy Committee[^<]*<BR>\s*<DIR>\s*<li>'
                      r'<a href="([^"]+)"', parts[i + 1], re.I)
        if m:
            out.append((_rbi_date(parts[i]), html.unescape(m.group(1))))
    return out


def parse_rbi_listing_form(html_: str) -> tuple[dict, int]:
    """The listing's hidden form fields plus its invisible submit button, and the fiscal-year id
    at the top of its year tree (2027 = 2026-27). Earlier years come back from a plain POST of
    these fields with `hdnYear` set — what the page's own GetYear() does."""
    fields = {}
    for m in re.finditer(r'<input[^>]+type="hidden"[^>]*>', html_):
        t = m.group(0)
        n, v = re.search(r'name="([^"]+)"', t), re.search(r'value="([^"]*)"', t)
        if n:
            fields[n.group(1)] = html.unescape(v.group(1)) if v else ""
    b = re.search(r'<input[^>]+id="btn"[^>]*>', html_)
    if b:
        n, v = re.search(r'name="([^"]+)"', b.group(0)), re.search(r'value="([^"]*)"', b.group(0))
        if n:
            fields[n.group(1)] = v.group(1) if v else ""
    y = re.search(r'class="year_tree"[^>]*\bid="(\d{4})"', html_)
    if not y or "__VIEWSTATE" not in fields:
        raise ValueError("RBI listing: no year tree or no form state")
    return fields, int(y.group(1))


def parse_rbi_resolution(html_: str) -> tuple[str, float, float | None]:
    """(iso decision date, repo rate decided, repo rate it replaced) from one MPC resolution
    press release. The RBI's moves take effect the day they are announced, which is how BIS
    dates them too. The rate it replaced is None when the text names a move but not its size."""
    t = _text(html_)
    d = re.search(r"Date\s*:\s*([A-Z][a-z]{2} \d{2}, \d{4})", t)
    if not d:
        raise ValueError("RBI resolution: no 'Date :' line")
    m = re.search(r"policy repo rate\b.{0,200}?\b(?:to|at)\s+(\d+(?:\.\d+)?)\s*per\s*cent", t, re.I)
    if not m:
        raise ValueError("RBI resolution: no 'policy repo rate ... X per cent' sentence")
    rate = _rbi_rate(m.group(1))
    lead = t[max(0, m.start() - 90):m.start()].lower()
    if "unchanged" in t[m.start():m.end()].lower() or re.search(r"\bkeep\b|\bretain", lead):
        return _rbi_date(d.group(1)), rate, rate
    direction = (1 if any(w in lead for w in _RBI_UP) else -1 if any(w in lead for w in _RBI_DOWN) else 0)
    size = re.search(r"policy repo rate\b[^.]{0,120}?\bby\s+(\d+)\s*(?:bps|basis points)", t, re.I)
    if direction and size:
        return _rbi_date(d.group(1)), rate, _rbi_rate(rate - direction * int(size.group(1)) / 100)
    return _rbi_date(d.group(1)), rate, (rate if not direction else None)


def rbi_in(start: str, fetch=None, post=None, today: str | None = None) -> list[tuple[str, float]]:
    """Policy repo rate as the step series of MPC decisions back to `start`, closed with today's
    reading from the home page — the same basis as BIS's D.IN (decision day = effective day).

    Reads: the home page (today's value), the Monetary Policy Statements listing (which press
    releases are MPC resolutions, and when), walking back a fiscal year at a time until a decision
    dated on or before `start` is in hand, and those resolutions (rate decided, size of the move).
    The home page must agree with the latest resolution or the whole series is refused — the two
    pages are the cross-check on each other. One anchor point is added the day before the oldest
    decision, at the rate that decision says it replaced, so BIS's last date can be matched even
    when it falls between meetings. The LAST point is the latest date the pages vouch for (today,
    or the latest decision if that is today): nothing after it is known. `fetch` and `post` are
    injectable for tests, and so is `today` (ISO; the real date when omitted).
    """
    fetch, post = fetch or _get, post or _post
    now = parse_rbi_home(fetch(RBI_HOME))
    listing = fetch(RBI_LISTING)
    items = parse_rbi_listing(listing)
    fields, fy = parse_rbi_listing_form(listing)
    back = 0
    while (not items or items[-1][0] > start) and back < RBI_MAX_YEARS_BACK:
        back += 1
        earlier = post(RBI_LISTING, {**fields, "hdnYear": str(fy - back)})
        items += parse_rbi_listing(earlier)
        fields = parse_rbi_listing_form(earlier)[0]
    decisions = []
    for _, url in items:               # newest first; stop once anchored at/before start
        d, rate, prev = parse_rbi_resolution(fetch(url))
        decisions.append((d, rate, prev))
        if d <= start:
            break
    if not decisions:
        raise ValueError("RBI listing: no MPC resolutions found")
    if abs(decisions[0][1] - now) > 0.011:
        raise ValueError(f"RBI home page shows {now} but the latest MPC resolution "
                         f"({decisions[0][0]}) decided {decisions[0][1]}; not splicing")
    series = [(d, r) for d, r, _ in decisions]
    oldest_d, _, oldest_prev = decisions[-1]
    if oldest_prev is not None:
        series.append(((date.fromisoformat(oldest_d) - timedelta(days=1)).isoformat(), oldest_prev))
    today = today or date.today().isoformat()
    if today > decisions[0][0]:
        series.append((today, now))
    return _clean(series)


if __name__ == "__main__":
    since = sys.argv[1] if len(sys.argv) > 1 else (date.today() - timedelta(days=120)).isoformat()
    for d, v in rbi_in(since):
        print(f"{d}  {v:.2f}")
