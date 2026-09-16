# NEXT — econvitals-data

## Next
- **Re-anchor the housing monitor's "Median sale price vs. 2019" tile (`valuation/median_vs_2019`).** FRED truncated every NAR series to a rolling 13 months on 2026-08-11 — `HOSMEDUSM052N` starts 2025-07 and ALFRED holds no earlier vintage — so `pct_vs_year` with `base_year: 2019` can never compute again. Either pin the 2019 base as a sourced constant in `builders/housing-monitor/housing_config.yaml`, or re-point the tile at a series that still carries history (Case-Shiller `CSUSHPISA`, relabelled). Done = the tile prints a live value and its `known_broken:` block comes off the config item. The row is declared known-broken until 2026-12-31, so the nightly Action is green meanwhile and fails again on that date if nothing has changed.

## Decided against
- **A same-day feed for the Bank of Japan row in the Stance Matrix** (Jason, 2026-09-16, when the Fed/ECB/BoE/BoC fast lane was built). The BoJ publishes no machine-readable series for the policy target; the only daily option is the actual uncollateralized overnight call rate off a 1.3MB Shift-JIS page at stat-search.boj.or.jp (`FM01'STRDCLUCON`, ~2 days behind), which is a market rate needing rounding to the nearest target step. More work than the other four combined, more fragile, worse answer. JP stays on BIS.
- **Per-bank feeds for the other seven banks** (AU, CH, SE, NO, NZ, CN, IN) — same date. Each has its own site and its own format; they move rarely, so twelve fragile sources buy little over the four majors. Revisit only if a specific row is visibly wrong when it matters.
