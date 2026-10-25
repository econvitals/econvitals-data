#!/usr/bin/env python3
"""
Each central bank's most recent monetary-policy DECISION, read from the bank's own press feed.

The Stance Matrix's rates come from BIS and the fast sources; this module adds the words: the
date, title and link of the latest decision announcement, plus the statement's text for the
downstream summarizer. Every source is the bank's official RSS/Atom feed where one exists, and
the bank's English press-release listing page where none does (verified 2026-10-25; Jason
approved reading the banks' own feeds).

  US  Federal Reserve     feeds/press_monetary.xml          "Federal Reserve issues FOMC statement"
  XM  ECB                 rss/press.html (all press)        "Monetary policy decisions"
                          -> 15 items, rarely reaches back to a decision; falls back to the
                             Governing Council decisions listing (index_include, per year)
  GB  Bank of England     rss/news                          "Bank Rate maintained/raised/... at X%"
  CA  Bank of Canada      press-releases feed (RSS 1.0)     "Bank of Canada maintains the policy rate at X%"
  AU  RBA                 rss-cb-media-releases.xml         "Statement by the Monetary Policy Board:
                          -> the feed holds ONE item; falls back  Monetary Policy Decision"
                             to the media-releases listing (per year)
  SE  Riksbank            en-gb/rss/press-releases/         "Policy rate unchanged/raised/... at X per cent"
  NZ  RBNZ                feeds/news                        "OCR increased/held/... "
                          -> rbnz.govt.nz answers 403 "Website unavailable" to any non-browser
                             client (whole site, 2026-10-25), so the excerpt is the feed's summary
  JP  Bank of Japan       en/rss/whatsnew.xml               "Statement on Monetary Policy" /
                          -> ~50 items covering a month;      "Change in the Guideline for Money
                             falls back to the Monetary Policy  Market Operations" (a rate change)
                             Releases listing (per year); the statement is a PDF, read by the
                             small extractor below
  IN  RBI                 pressreleases_rss.xml             "Resolution of the Monetary Policy Committee"
                          -> 10 items, all one day; falls back to the Monetary Policy Statements
                             listing fast_sources already reads (fixtures/rbi_listing_*.html)
  CH  SNB                 public/rss/en/mopo                "Monetary policy assessment of 24 September 2026"
  NO  Norges Bank         en/rss-feeds/Press-releases---Norges-Bank/   "Policy rate raised to 4.50%"
  CN  PBoC                NO FEED. The English LPR announcements listing page; the monthly
                          "Announcement on Loan Prime Rate (October 20, 2026)" is the decision.

Rules of the module: standard library only; the plain "jfmacro-lab/1.0" user agent; a failing
source never raises out of `latest_decision` or `all_decisions` — it comes back as a result with
`error` set. `fetch` is injectable (callable(url) -> str, or bytes for a PDF) so the tests run on
saved copies under fixtures/feeds/. Dates are the publisher's own date as written in the feed —
no timezone conversion — because a decision is "the September 17 decision" in the bank's own
calendar, and every feed here stamps items in local time or in a zone whose date agrees.
"""

from __future__ import annotations

import html
import re
import urllib.parse
import urllib.request
import zlib
from datetime import date, datetime

from fast_sources import RBI_LISTING

UA = {"User-Agent": "jfmacro-lab/1.0"}
TIMEOUT = 45
EXCERPT_CHARS = 1500
TEXT_CHARS = 12000


def _get(url: str):
    """Text as str; a PDF as bytes (the BoJ statements)."""
    raw = urllib.request.urlopen(
        urllib.request.Request(url, headers=UA), timeout=TIMEOUT
    ).read()
    if raw[:5] == b"%PDF-":
        return raw
    return raw.decode("utf-8", "replace")


# ------------------------------------------------------------------ per-bank table
# title   : matches the decision release and nothing else on that feed (minutes, speeches,
#           projections, auctions, schedules all miss) — proven against the saved feeds in
#           test_feeds.py.
# listing : fallback page(s) when the feed is too short to hold the last decision; "{year}" is
#           filled with this year, then last year (a January run). `rows` is the regex that
#           pulls (date, url, title) off that page.
# start   : where the statement's prose begins on its page, so the excerpt skips the chrome.
# container : the HTML block that holds the statement, when <main>/<article> is not it.
_ECB_LISTING = "https://www.ecb.europa.eu/press/govcdec/mopo/{year}/html/index_include.en.html"
_RBA_LISTING = "https://www.rba.gov.au/media-releases/{year}/"
_BOJ_LISTING = "https://www.boj.or.jp/en/mopo/mpmdeci/mpr_{year}/index.htm"
_PBOC_LPR = "https://www.pbc.gov.cn/en/3688229/3688335/3730276/3883798/index.html"

FEEDS: dict[str, dict] = {
    "US": {
        "url": "https://www.federalreserve.gov/feeds/press_monetary.xml",
        "label": "Federal Reserve monetary policy press releases (RSS)", "kind": "rss",
        "title": re.compile(r"^Federal Reserve issues FOMC statement\b"),
        # the statement sits in the 8-column div after the heading; the 4-column div beside it is chrome
        "container": re.compile(r'(?s)<div class="col-xs-12 col-sm-8 col-md-8">.*?(?=<div class="col-xs-12 col-sm-4 col-md-4">)'),
        "start": re.compile(r"The Federal Open Market Committee|For release at|Recent indicators|"
                            r"Information received|The Committee"),
    },
    "XM": {
        "url": "https://www.ecb.europa.eu/rss/press.html",
        "label": "European Central Bank press releases (RSS) + Governing Council decisions listing",
        "kind": "rss",
        "title": re.compile(r"^Monetary policy decisions$"),
        "listing": _ECB_LISTING,
        "rows": re.compile(r'(?s)<dt isoDate="(?P<date>\d{4}-\d{2}-\d{2})">.*?<div class="title">'
                           r'<a href="(?P<url>[^"]+)"[^>]*>(?P<title>[^<]*)</a>'),
        "start": re.compile(r"The Governing Council today"),
    },
    "GB": {
        "url": "https://www.bankofengland.co.uk/rss/news",
        "label": "Bank of England news (RSS)", "kind": "rss",
        "title": re.compile(r"^Bank Rate (maintained|increased|raised|reduced|cut|lowered|held)\b", re.I),
        "start": re.compile(r"Monetary Policy Summary, [A-Z][a-z]+ \d{4}"),
    },
    "CA": {
        "url": "https://www.bankofcanada.ca/content_type/press-releases/feed/",
        "label": "Bank of Canada press releases (RSS)", "kind": "rss",
        "title": re.compile(r"^Bank of Canada (maintains|holds|lowers|cuts|reduces|raises|increases)\b"
                            r".*\bpolicy (interest )?rate", re.I),
        "start": re.compile(r"The Bank of Canada today"),
    },
    "AU": {
        "url": "https://www.rba.gov.au/rss/rss-cb-media-releases.xml",
        "label": "Reserve Bank of Australia media releases (RSS) + media-releases listing",
        "kind": "rss",
        "title": re.compile(r"^Statement by the (Monetary Policy Board|Reserve Bank Board): "
                            r"Monetary Policy Decision"),
        "listing": _RBA_LISTING,
        "rows": re.compile(r'(?s)<article id="mr-[^"]+".*?<a href="(?P<url>[^"]+)"[^>]*>\s*'
                           r'<span itemprop="headline">(?P<title>[^<]*)</span>.*?'
                           r'<time datetime="(?P<date>[^"]+)"'),
        "start": re.compile(r"At its meeting today"),
    },
    "SE": {
        "url": "https://www.riksbank.se/en-gb/rss/press-releases/",
        "label": "Sveriges Riksbank press releases (RSS)", "kind": "rss",
        "title": re.compile(r"^Policy rate (unchanged|raised|increased|cut|lowered|reduced)\b", re.I),
        "start": re.compile(r"The Executive Board"),
    },
    "NZ": {
        "url": "https://www.rbnz.govt.nz/feeds/news",
        "label": "Reserve Bank of New Zealand news (RSS)", "kind": "rss",
        "title": re.compile(r"^(OCR\b|Official Cash Rate\b)", re.I),
        "start": None,
    },
    "JP": {
        "url": "https://www.boj.or.jp/en/rss/whatsnew.xml",
        "label": "Bank of Japan What's New (RSS) + Monetary Policy Releases listing", "kind": "rss",
        "title": re.compile(r"^(Statement on Monetary Policy|Change in the Guideline for Money Market "
                            r"Operations)\b"),
        "listing": _BOJ_LISTING,
        "rows": re.compile(r'(?s)<tr>\s*<td>(?P<date>[^<]+)</td>\s*<td><a href="(?P<url>[^"]+)"[^>]*>'
                           r'(?P<title>[^<]*)</a>'),
        "start": re.compile(r"[A-Z][a-z]+ \d{1,2}, \d{4}\s+Bank of Japan\s"),   # past the page number
    },
    "IN": {
        "url": "https://www.rbi.org.in/pressreleases_rss.xml",
        "label": "Reserve Bank of India press releases (RSS) + Monetary Policy Statements listing",
        "kind": "rss",
        "title": re.compile(r"Resolution of the Monetary Policy Committee"),
        "listing": RBI_LISTING,
        # one date header, then the MPC resolution inside that same date block (the tempered
        # lookahead refuses to run across the next date header)
        "rows": re.compile(r'(?s)<td class="tableheader"><b>(?P<date>[A-Z][a-z]{2} \d{2}, \d{4})</b>'
                           r'(?:(?!<td class="tableheader"><b>[A-Z][a-z]{2} \d{2}, \d{4}).)*?'
                           r'(?P<title>Resolution of the Monetary Policy Committee[^<]*)<BR>\s*<DIR>\s*'
                           r'<li><a href="(?P<url>[^"]+)"', re.I),
        # the release sits in #doublescroll; the year/month archive tree follows it
        "container": re.compile(r'(?s)<div id="doublescroll">.*?(?=<div class="grid_1 archives)'),
        "start": re.compile(r"Monetary Policy Decisions|The Monetary Policy Committee \(MPC\) held"),
    },
    "CH": {
        "url": "https://www.snb.ch/public/rss/en/mopo",
        "label": "Swiss National Bank monetary policy releases (RSS)", "kind": "rss",
        # "2026-09-24 - Monetary policy assessment of 24 September 2026"; NOT "Monetary policy
        # assessment of September 2026: Summary of discussions" (the minutes, four weeks later)
        "title": re.compile(r"^(?:\d{4}-\d{2}-\d{2} - )?Monetary policy assessment of \d{1,2} [A-Z][a-z]+ \d{4}\s*$"),
        "start": re.compile(r"Swiss National Bank (leaves|keeps|lowers|cuts|raises|tightens|eases)|"
                            r"The Swiss National Bank is"),
    },
    "NO": {
        "url": "https://www.norges-bank.no/en/rss-feeds/Press-releases---Norges-Bank/",
        "label": "Norges Bank press releases (RSS)", "kind": "rss",
        "title": re.compile(r"^Policy rate (raised|increased|kept unchanged|unchanged|lowered|cut|reduced)\b", re.I),
        "start": re.compile(r"At its meeting on"),
    },
    "CN": {
        # The PBoC publishes no RSS feed (none on pbc.gov.cn/en, 2026-10-25). The monthly LPR
        # announcement listing is the official record of the decision, so that page is the source.
        "url": _PBOC_LPR,
        "label": "People's Bank of China — Loan Prime Rate announcements (listing page, no feed)",
        "kind": "listing",
        "title": re.compile(r"^Announcement on Loan Prime Rate \("),
        "rows": re.compile(r'<a href="(?P<url>[^"]+)"[^>]*title="(?P<title>Announcement on Loan Prime '
                           r'Rate \((?P<date>[^)]+)\))"'),
        "container": re.compile(r'(?s)<div class="xiangxiDetial".*'),
        "start": re.compile(r"Under the authorization|The National Interbank Funding Center"),
    },
}


# ------------------------------------------------------------------- feed parsing
_ITEM = re.compile(r"<(item|entry)\b[^>]*>(.*?)</\1>", re.S | re.I)
_MONTHS = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1)}


def _clean(s: str | None) -> str:
    """CDATA off, tags off, entities in, whitespace collapsed."""
    if not s:
        return ""
    s = re.sub(r"<!\[CDATA\[(.*?)\]\]>", r"\1", s, flags=re.S)
    s = re.sub(r"<[^>]+>", " ", s)
    return re.sub(r"\s+", " ", html.unescape(s)).replace("\xa0", " ").strip()


def _field(block: str, *tags: str) -> str | None:
    for tag in tags:
        m = re.search(rf"<{tag}\b[^>]*>(.*?)</{tag}>", block, re.S | re.I)
        if m:
            return m.group(1)
    return None


def _link(block: str) -> str:
    """RSS <link>text</link>, or Atom <link href=.../> (the alternate one, or the first)."""
    for m in re.finditer(r"<link\b([^>]*?)(/>|>(.*?)</link>)", block, re.S | re.I):
        attrs, text = m.group(1), m.group(3)
        href = re.search(r'href="([^"]+)"', attrs)
        rel = re.search(r'rel="([^"]+)"', attrs)
        if href and (not rel or rel.group(1) == "alternate"):
            return html.unescape(href.group(1)).strip()
        if text and _clean(text):
            return _clean(text)
    return ""


def parse_date(s: str | None) -> str | None:
    """The date as written, to ISO. Handles every form the feeds and listings use:
    'Tue, 13 Oct 2026 18:00:00 GMT', 'Fri, 23 Oct 2026 19:25:00' (RBI, no zone),
    '2026-09-10T11:00:09+00:00', '2026-09-24T07:30:00Z', '2026-09-29T14:30+10:00',
    '29 September 2026', 'October 20, 2026', 'Oct 07, 2026', 'Sept. 18, 2026'. None if none."""
    s = _clean(s)
    if not s:
        return None
    m = re.search(r"(\d{4})-(\d{2})-(\d{2})", s)
    if m:
        y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
    else:
        m = re.search(r"\b(\d{1,2})\s+([A-Za-z]{3,9})\.?,?\s+(\d{4})\b", s)        # 24 September 2026
        if m:
            d, mo, y = int(m.group(1)), _MONTHS.get(m.group(2)[:3].lower()), int(m.group(3))
        else:
            m = re.search(r"\b([A-Za-z]{3,9})\.?\s+(\d{1,2}),?\s+(\d{4})\b", s)    # October 20, 2026
            if not m:
                return None
            mo, d, y = _MONTHS.get(m.group(1)[:3].lower()), int(m.group(2)), int(m.group(3))
    try:
        return date(y, mo, d).isoformat() if mo else None
    except (TypeError, ValueError):
        return None


def parse_feed(xml: str) -> list[dict]:
    """[{title, url, date, summary}] in feed order (newest first on every feed here).
    RSS 2.0, RSS 1.0 (RDF; Bank of Canada, RBA) and Atom all come through the same regexes."""
    out = []
    for m in _ITEM.finditer(xml):
        block = m.group(2)
        out.append({
            "title": _clean(_field(block, "title")),
            "url": _link(block),
            "date": parse_date(_field(block, "pubDate", "dc:date", "published", "updated", "date")),
            "summary": _clean(_field(block, "description", "summary", "content")),
        })
    return out


def parse_listing(code: str, page: str, base_url: str) -> list[dict]:
    """The same shape as parse_feed, off a bank's listing page, with that bank's `rows` regex."""
    rows = FEEDS[code].get("rows")
    out = []
    for m in rows.finditer(page) if rows else []:
        title = re.sub(r"\s*\[PDF[^\]]*\]\s*$", "", _clean(m.group("title")))   # BoJ "[PDF 204KB]"
        out.append({
            "title": title,
            "url": urllib.parse.urljoin(base_url, html.unescape(m.group("url"))),
            "date": parse_date(m.group("date")),
            "summary": "",
        })
    return out


def is_decision(code: str, title: str) -> bool:
    return bool(FEEDS[code]["title"].search(title or ""))


def _pick(code: str, items: list[dict]) -> dict | None:
    """The newest decision item; feed order breaks a tie or an undated item."""
    hits = [it for it in items if is_decision(code, it["title"])]
    if not hits:
        return None
    return max(hits, key=lambda it: it["date"] or "")


def listing_urls(code: str, today: date | None = None) -> list[str]:
    tmpl = FEEDS[code].get("listing")
    if not tmpl:
        return []
    if "{year}" not in tmpl:
        return [tmpl]
    y = (today or date.today()).year
    return [tmpl.format(year=y), tmpl.format(year=y - 1)]


# ---------------------------------------------------------------- statement text
# not <form>: ASP.NET sites (the RBI) wrap the whole page body in one
_DROP = re.compile(r"(?is)<(script|style|noscript|svg|template|iframe|head|header|footer|nav|aside)\b.*?</\1>")
_BLOCK = re.compile(r"(?i)</(p|div|h[1-6]|li|tr|td|th|section|article|blockquote|dt|dd)>|<br\s*/?>|<(p|h[1-6]|li|tr)\b[^>]*>")


def html_text(page: str, container: re.Pattern | None = None) -> str:
    """Main body prose: scripts/nav/header/footer out, block ends to newlines, tags out,
    entities in, whitespace collapsed. `container` picks the statement block when the page's
    <main> is not it; otherwise <main>, then <article>, then <body>."""
    page = re.sub(r"(?s)<!--.*?-->", " ", page)
    block = None
    if container:
        m = container.search(page)
        block = m.group(0) if m else None
    if block is None:
        for tag in ("main", "article", "body"):
            m = re.search(rf"(?is)<{tag}\b.*?</{tag}>", page) or re.search(rf"(?is)<{tag}\b.*", page)
            if m:
                block = m.group(0)
                break
    block = _DROP.sub(" ", block if block is not None else page)
    block = _BLOCK.sub("\n", block)
    t = html.unescape(re.sub(r"<[^>]+>", " ", block)).replace("\xa0", " ")
    t = re.sub(r"[ \t\r\f\v]+", " ", t)
    return re.sub(r"\s*\n\s*", "\n", t).strip()


def statement_text(doc, code: str) -> str:
    """Full statement prose from the fetched document (HTML str, or PDF bytes), cut to where
    the bank's own words begin when the `start` marker is found."""
    cfg = FEEDS[code]
    if isinstance(doc, str) and doc.startswith("%PDF-"):
        doc = doc.encode("latin-1", "replace")
    if isinstance(doc, bytes):
        t = pdf_text(doc) if doc[:5] == b"%PDF-" else html_text(doc.decode("utf-8", "replace"), cfg.get("container"))
    else:
        t = html_text(doc, cfg.get("container"))
    start = cfg.get("start")
    if start:
        m = start.search(t)
        if m:
            t = t[m.start():]
    return t.strip()


# ------------------------------------------------------- a small PDF text reader
# Only the Bank of Japan's statements need this (they are Word-made PDFs: WinAnsi TrueType fonts
# for the plain runs, Identity-H CID fonts with ToUnicode maps for the bold ones). It handles
# FlateDecode streams, object streams, page order from the Pages tree, per-page font resources,
# ToUnicode bfchar/bfrange, and the Tj/TJ/'/" operators. It is not a general PDF library.
_REF = re.compile(rb"(\d+)\s+0\s+R")
_TOK = re.compile(rb"\((?:\\.|[^\\)])*\)|<[0-9A-Fa-f\s]*>|\[|\]|/[^\s/\[\]<>(){}]+|[-+]?\d*\.?\d+|[A-Za-z'\"*]+")
_ESC = {b"n": b"\n", b"r": b"\r", b"t": b"\t", b"b": b"\b", b"f": b"\f", b"(": b"(", b")": b")", b"\\": b"\\"}


def _pdf_objects(data: bytes) -> dict[int, bytes]:
    objs: dict[int, bytes] = {}
    for m in re.finditer(rb"(?<![\d])(\d+)\s+0\s+obj\b(.*?)endobj", data, re.S):
        objs[int(m.group(1))] = m.group(2)
    for body in list(objs.values()):                       # unpack object streams
        if b"/ObjStm" not in body:
            continue
        d = _pdf_stream(body)
        first = int(re.search(rb"/First\s+(\d+)", body).group(1))
        n = int(re.search(rb"/N\s+(\d+)", body).group(1))
        hdr = d[:first].split()
        for i in range(n):
            num, off = int(hdr[2 * i]), int(hdr[2 * i + 1])
            end = int(hdr[2 * i + 3]) if i + 1 < n else len(d) - first
            objs.setdefault(num, d[first + off:first + end])
    return objs


def _pdf_stream(body: bytes) -> bytes:
    m = re.search(rb"stream\r?\n(.*?)\r?\nendstream", body, re.S)
    if not m:
        return b""
    raw = m.group(1)
    if b"/FlateDecode" in body[:m.start()]:
        try:
            return zlib.decompress(raw)
        except zlib.error:
            return zlib.decompressobj().decompress(raw)
    return raw


def _pdf_value(body: bytes, key: bytes, objs: dict) -> bytes:
    """The value of /key in a dictionary body, with an indirect reference resolved; a nested
    dictionary or array comes back whole (balanced)."""
    m = re.search(rb"/" + key + rb"(?![A-Za-z0-9])\s*", body)
    if not m:
        return b""
    rest = body[m.end():]
    r = _REF.match(rest)
    if r:
        return objs.get(int(r.group(1)), b"")
    if rest[:2] == b"<<" or rest[:1] == b"[":
        open_, close = (b"<<", b">>") if rest[:2] == b"<<" else (b"[", b"]")
        depth, i = 0, 0
        while i < len(rest):
            if rest.startswith(open_, i):
                depth += 1
                i += len(open_)
            elif rest.startswith(close, i):
                depth -= 1
                i += len(close)
                if depth == 0:
                    return rest[:i]
            else:
                i += 1
        return rest
    return re.match(rb"[^\s/>\]]*", rest).group(0)


def _pdf_pages(objs: dict) -> list[int]:
    roots = [n for n, b in objs.items() if re.search(rb"/Type\s*/Pages\b", b) and b"/Parent" not in b]
    order: list[int] = []

    def walk(n: int):
        b = objs.get(n, b"")
        if re.search(rb"/Type\s*/Page\b", b):
            order.append(n)
        else:
            for k in _REF.findall(_pdf_value(b, b"Kids", {})):
                walk(int(k))
    for r in roots:
        walk(r)
    return order or sorted(n for n, b in objs.items() if re.search(rb"/Type\s*/Page\b", b))


def _cmap(stream: bytes) -> tuple[dict[int, str], int]:
    """ToUnicode CMap -> ({code: text}, code length in bytes)."""
    table: dict[int, str] = {}
    width = 2
    cs = re.search(rb"begincodespacerange\s*<([0-9A-Fa-f]+)>", stream)
    if cs:
        width = max(1, len(cs.group(1)) // 2)
    uni = lambda h: bytes.fromhex(h.decode()).decode("utf-16-be", "replace")  # noqa: E731
    for sect in re.findall(rb"beginbfchar(.*?)endbfchar", stream, re.S):
        for src, dst in re.findall(rb"<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>", sect):
            table[int(src, 16)] = uni(dst)
    for sect in re.findall(rb"beginbfrange(.*?)endbfrange", stream, re.S):
        for lo, hi, dst in re.findall(rb"<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>\s*(<[0-9A-Fa-f]+>|\[[^\]]*\])", sect):
            lo_i, hi_i = int(lo, 16), int(hi, 16)
            if dst[:1] == b"[":
                for k, d in enumerate(re.findall(rb"<([0-9A-Fa-f]+)>", dst)):
                    table[lo_i + k] = uni(d)
            else:
                base = int(dst[1:-1], 16)
                for c in range(lo_i, min(hi_i, lo_i + 65535) + 1):
                    table[c] = chr(base + c - lo_i)
    return table, width


def _font_decoder(font: bytes, objs: dict):
    tu = _pdf_value(font, b"ToUnicode", objs)
    table, width = (_cmap(_pdf_stream(tu)) if tu else ({}, 1))
    if not tu and re.search(rb"/Subtype\s*/Type0", font):
        width = 2

    def decode(s: bytes) -> str:
        if table:
            codes = [int.from_bytes(s[i:i + width], "big") for i in range(0, len(s) - width + 1, width)]
            return "".join(table.get(c, "�" if width == 2 else chr(c)) for c in codes)
        return s.decode("cp1252", "replace")
    return decode


def _pdf_string(tok: bytes) -> bytes:
    if tok[:1] == b"<":
        h = re.sub(rb"\s+", b"", tok[1:-1])
        return bytes.fromhex(h.decode() + ("0" if len(h) % 2 else ""))
    out, i, s = bytearray(), 0, tok[1:-1]
    while i < len(s):
        c = s[i:i + 1]
        if c == b"\\" and i + 1 < len(s):
            nxt = s[i + 1:i + 2]
            if nxt.isdigit():
                oct_ = re.match(rb"[0-7]{1,3}", s[i + 1:]).group(0)
                out += bytes([int(oct_, 8) & 0xFF])
                i += 1 + len(oct_)
                continue
            out += _ESC.get(nxt, nxt)
            i += 2
        else:
            out += c
            i += 1
    return bytes(out)


def pdf_text(data: bytes) -> str:
    """Plain text of a simple text PDF, page by page; a space between runs, a newline when the
    text matrix moves to a new line. Garbled fonts come out as U+FFFD rather than raising."""
    objs = _pdf_objects(data)
    out: list[str] = []
    for pn in _pdf_pages(objs):
        page = objs[pn]
        res = _pdf_value(page, b"Resources", objs)
        fonts = _pdf_value(res, b"Font", objs)
        decoders = {}
        for name, ref in re.findall(rb"/([^\s/<>\[\]]+)\s+(\d+)\s+0\s+R", fonts):
            decoders[name] = _font_decoder(objs.get(int(ref), b""), objs)
        contents = _pdf_value(page, b"Contents", objs)
        refs = _REF.findall(page[page.find(b"/Contents"):]) if b"/Contents" in page else []
        if contents[:1] == b"[":
            refs = _REF.findall(contents)
        stream = b"\n".join(_pdf_stream(objs.get(int(r), b"")) for r in refs) if refs else _pdf_stream(contents)
        decode = None
        operands: list[bytes] = []
        in_array, array = False, []
        last_y = None
        for tok in _TOK.findall(stream):
            if tok == b"[":
                in_array, array = True, []
            elif tok == b"]":
                in_array = False
                operands.append(b"[")
            elif in_array:
                array.append(tok)
            elif tok[:1] in b"(</" or tok[:1].isdigit() or tok[:1] in b"-+.":
                operands.append(tok)
            else:                                        # an operator
                if tok == b"Tf" and len(operands) >= 2:
                    decode = decoders.get(operands[-2][1:])
                elif tok in (b"Tj", b"'", b'"') and operands and decode:
                    if tok != b"Tj":
                        out.append("\n")
                    out.append(decode(_pdf_string(operands[-1])))
                elif tok == b"TJ" and decode:
                    for el in array:
                        if el[:1] in b"(<":
                            out.append(decode(_pdf_string(el)))
                        else:
                            try:
                                if float(el) < -200:
                                    out.append(" ")
                            except ValueError:
                                pass
                elif tok in (b"Td", b"TD", b"Tm") and len(operands) >= 2:
                    try:
                        y = float(operands[-1])
                    except ValueError:
                        y = None
                    if tok == b"Tm":        # a new line on a y move; runs on one line join
                        if last_y is not None and y is not None and abs(y - last_y) > 1:  # directly —
                            out.append("\n")                                         # Word writes
                        last_y = y                                                   # spaces as glyphs
                    else:
                        out.append("\n" if y and abs(y) > 1 else " ")
                elif tok == b"T*":
                    out.append("\n")
                operands = []
        out.append("\n")
    t = "".join(out).replace("�", "")
    t = re.sub(r"[ \t]+", " ", t)
    return re.sub(r"\s*\n\s*", "\n", t).strip()


# ------------------------------------------------------------------------ the API
def _result(code: str, **kw) -> dict:
    base = {"code": code, "source_url": FEEDS.get(code, {}).get("url"), "label": FEEDS.get(code, {}).get("label"),
            "announced": None, "title": None, "url": None, "excerpt": "", "text": "",
            "items_seen": 0, "error": None, "note": None,
            "fetched_at": datetime.now().astimezone().isoformat(timespec="seconds")}
    base.update(kw)
    return base


def latest_decision(code: str, fetch=None, today: date | None = None) -> dict:
    """The bank's most recent decision announcement off its own feed (then its listing page
    when the feed is too short), with the statement's text. Never raises: anything that goes
    wrong is the `error` string, and `items_seen` says how far the read got."""
    fetch = fetch or _get
    if code not in FEEDS:
        return _result(code, error=f"no feed configured for {code!r}")
    cfg = FEEDS[code]
    seen, note, source_url, hit = 0, None, cfg["url"], None
    try:
        if cfg["kind"] == "rss":
            items = parse_feed(fetch(cfg["url"]))
            seen += len(items)
            hit = _pick(code, items)
            if hit is None and cfg.get("listing"):
                for lurl in listing_urls(code, today):
                    rows = parse_listing(code, fetch(lurl), lurl)
                    seen += len(rows)
                    hit = _pick(code, rows)
                    if hit:
                        source_url = lurl
                        note = (f"the feed's {len(items)} items held no decision; "
                                f"read the bank's listing page instead")
                        break
        else:
            rows = parse_listing(code, fetch(cfg["url"]), cfg["url"])
            seen += len(rows)
            hit = _pick(code, rows)
        if hit is None:
            return _result(code, source_url=source_url, items_seen=seen,
                           error=f"no decision item among {seen} items")
        text, excerpt = "", ""
        try:
            text = statement_text(fetch(hit["url"]), code)
        except Exception as e:                           # the page, not the decision, failed
            note = f"statement page not read ({type(e).__name__}: {str(e)[:120]}); excerpt is the feed's summary"
        if not text.strip():
            text = hit.get("summary") or ""
            note = note or "statement page had no readable text; excerpt is the feed's summary"
        text = text[:TEXT_CHARS]
        excerpt = text[:EXCERPT_CHARS]
        return _result(code, source_url=source_url, announced=hit["date"], title=hit["title"],
                       url=hit["url"], excerpt=excerpt, text=text, items_seen=seen, note=note)
    except Exception as e:
        return _result(code, source_url=source_url, items_seen=seen,
                       error=f"{type(e).__name__}: {str(e)[:200]}")


def all_decisions(fetch=None, today: date | None = None) -> dict[str, dict]:
    """Every bank in FEEDS; a bank that fails is a result with `error`, never an exception."""
    out = {}
    for code in FEEDS:
        try:
            out[code] = latest_decision(code, fetch, today)
        except Exception as e:                           # belt and braces: latest_decision guards itself
            out[code] = _result(code, error=f"{type(e).__name__}: {str(e)[:200]}")
    return out


if __name__ == "__main__":
    import json
    import sys
    codes = sys.argv[1:] or list(FEEDS)
    for c in codes:
        r = latest_decision(c)
        print(json.dumps({k: (v[:160] + "…" if isinstance(v, str) and len(v) > 160 else v)
                          for k, v in r.items()}, ensure_ascii=False, indent=1))
