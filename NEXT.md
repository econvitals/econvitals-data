# NEXT — econvitals-data

## Next

## Decided against
- **A same-day feed for the Bank of Japan row in the Stance Matrix** (Jason, 2026-09-16, when the Fed/ECB/BoE/BoC fast lane was built). The BoJ publishes no machine-readable series for the policy target; the only daily option is the actual uncollateralized overnight call rate off a 1.3MB Shift-JIS page at stat-search.boj.or.jp (`FM01'STRDCLUCON`, ~2 days behind), which is a market rate needing rounding to the nearest target step. More work than the other four combined, more fragile, worse answer. JP stays on BIS.
- **Per-bank feeds for the other six banks** (AU, CH, SE, NO, NZ, CN) — same date. Each has its own site and its own format; they move rarely, so eleven fragile sources buy little over the five. Revisit only if a specific row is visibly wrong when it matters. (India was, 2026-10-21: BIS stopped relaying the RBI after 2026-07-23 and missed the October hike. It now has one — `fast_sources.rbi_in`, the RBI's home page + MPC resolution press releases, at Jason's word reversing his 2026-10-09 no-scrape ruling; the `manual_rate` hand entry it carried for a day is retired and stays only as the documented emergency fallback.)
