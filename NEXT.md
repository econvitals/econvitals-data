# NEXT — econvitals-data

## Next
- **Re-anchor the housing monitor's "Median sale price vs. 2019" tile (`valuation/median_vs_2019`).** FRED truncated every NAR series to a rolling 13 months on 2026-08-11 — `HOSMEDUSM052N` starts 2025-07 and ALFRED holds no earlier vintage — so `pct_vs_year` with `base_year: 2019` can never compute again. Either pin the 2019 base as a sourced constant in `builders/housing-monitor/housing_config.yaml`, or re-point the tile at a series that still carries history (Case-Shiller `CSUSHPISA`, relabelled). Done = the nightly Action reports 0 failed rows and the tile prints a live value.
