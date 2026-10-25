# Archive — econvitals-data

Finished items that have rolled off `NEXT.md`. **Nothing here is queued work.** The /admin
next-steps panel reads `NEXT.md` and only `NEXT.md`, so this file is invisible to it by
design — which is why finished investigations belong here and never under `## Findings`.

Written by `jobhost-ops/next_archive.py`, daily. Newest sweep first.

## Swept 2026-10-25

### Next
- ~~**Stance Matrix: enter the Reserve Bank of New Zealand's decision dates in `builders/central-bank-stance/meetings.yaml`.** rbnz.govt.nz answers 403 to every automated fetch (2026-10-25), so `NZ.dates` is empty and the page's next-meeting cell for New Zealand reads as not published; the 6:10 mail names it daily (`calendar_running_out`). Done = the OCR decision dates through 2027 copied from the RBNZ's monetary-policy decision-dates page (open it in a browser; the URL is in the file) with `fetched` bumped, and the mail line gone.~~

## Swept 2026-10-22

### Next
- ~~Stance Matrix, India: after each RBI MPC decision (next: early December 2026) re-enter `manual_rate` for `IN` in `builders/central-bank-stance/cb_matrix_config.yaml` from the RBI's "Resolution of the Monetary Policy Committee" press release (value, decision date, `verified` = the day you read rbi.org.in's "Policy Repo Rate" box, the press-release URL), then run `python3 builders/central-bank-stance/build.py` and commit config + data.json. Done = the India row shows the new rate and date. The reminder is the alarm: once `verified` is more than 75 days old, the Action's last step (`build.py --check-stale`) fails the run after the publish and GitHub emails the owner naming the bank and date — bumping `verified` clears it. When the build prints "BIS has caught up to the hand entry", delete the block instead.~~ (Superseded the same day: India is read from the RBI's own site, `fast_sources.rbi_in`; the hand entry is retired.)

## Swept 2026-09-21

### Next
- ~~**Re-anchor the housing monitor's "Median sale price vs. 2019" tile (`valuation/median_vs_2019`).**~~ **DONE 2026-09-20 — re-pointed at Case-Shiller, relabelled, `known_broken:` off, and the row is live (`+58`, data through Jun 2026).** The other closure this bullet offered — pinning NAR's 2019 base as a sourced constant — was tried first and dropped: ALFRED answers "the series does not exist in ALFRED" for `HOSMEDUSM052N`, and NAR does not publish its 2019 monthly medians free, so the constant would have been twelve hand-typed numbers with no provenance. `CSUSHPISA` carries the full record, already feeds the two rows above it, and is repeat-sales, which is a better answer for a valuation tile than a median that mix shift can move. The tile is therefore "Home prices vs. 2019", not "Median sale price" — those are different measures and the label has to say which is on the page. The builder now reports `failed: 0, known_broken: 0`.
