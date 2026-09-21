# Archive — econvitals-data

Finished items that have rolled off `NEXT.md`. **Nothing here is queued work.** The /admin
next-steps panel reads `NEXT.md` and only `NEXT.md`, so this file is invisible to it by
design — which is why finished investigations belong here and never under `## Findings`.

Written by `jobhost-ops/next_archive.py`, daily. Newest sweep first.

## Swept 2026-09-21

### Next
- ~~**Re-anchor the housing monitor's "Median sale price vs. 2019" tile (`valuation/median_vs_2019`).**~~ **DONE 2026-09-20 — re-pointed at Case-Shiller, relabelled, `known_broken:` off, and the row is live (`+58`, data through Jun 2026).** The other closure this bullet offered — pinning NAR's 2019 base as a sourced constant — was tried first and dropped: ALFRED answers "the series does not exist in ALFRED" for `HOSMEDUSM052N`, and NAR does not publish its 2019 monthly medians free, so the constant would have been twelve hand-typed numbers with no provenance. `CSUSHPISA` carries the full record, already feeds the two rows above it, and is repeat-sales, which is a better answer for a valuation tile than a median that mix shift can move. The tile is therefore "Home prices vs. 2019", not "Median sale price" — those are different measures and the label has to say which is on the page. The builder now reports `failed: 0, known_broken: 0`.
