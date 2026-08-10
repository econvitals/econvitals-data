# manual.json — where each hand-entered number comes from

Everything in `manual.json` is a value with no automatable source yet (or a judgment
call that stays human). Update at monthly production (target ≈ the 20th); each block
carries its own `asof`. After editing, re-run `python3 fetch_data.py` and commit both
files.

## btos — Census Business Trends and Outlook Survey, AI use

- **What:** share of firms answering yes to "used AI in the last two weeks"
  (national, all firms). The spec's headline Adoption metric.
- **Where:** https://www.census.gov/hfp/btos/data.html → latest biweekly national
  file. The AI questions are in the AI supplement section.
- **Enter:** `share_pct` (latest), `delta_3m_pp` (change vs ~3 months earlier, in
  percentage points), `asof` ("Jun 2026" style), and append `[decimal_year, value]`
  to `series` (decimal year = year + (month − 0.5)/12; one point per month is fine
  even though the survey is biweekly).
- Automation is the Phase-3 scrape; manual until then.

## capex — hyperscaler capital expenditure

- **What:** combined quarterly capex of MSFT, GOOGL, AMZN, META, ORCL, in $bn,
  from 10-Q/10-K cash-flow statements ("purchases of property and equipment").
- **Enter:** append `{"q": "2026Q2", "bn": 123.4}` each earnings season.
- The spec's "macro@ repository" for this does not exist — this file IS the
  repository.

## scoreboard — the three third-party labor trackers

Re-plotting terms for Yale and Stanford are unconfirmed (spec §8.6), so these rows
show the summary statistic + a link, not a reproduced chart.

- **ybl_dissimilarity** — Yale Budget Lab's monthly AI/labor update:
  https://budgetlab.yale.edu (dissimilarity index vs. historical tech-change
  baselines).
- **ybl_quintiles** — since Phase 2 this row is fed automatically from
  `inhouse.json` (the in-house CPS replication); the manual slot below is only
  a fallback used if `inhouse.json` is absent.
- **canaries_early_career / canaries_auto_aug** — Stanford DEL–ADP Canaries
  dashboard: https://indicators.stanford.edu (early-career employment index for
  the top exposure quintile; automation-vs-augmentation split).
- **challenger_ai_share** — Challenger, Gray & Christmas monthly job-cuts report:
  AI-cited announcements as a share of total announced cuts.
- **Enter per row:** `latest` (display string, e.g. "97.2"), `asof`, `delta`
  (numeric, vs last issue), `delta_units` (e.g. "pts vs last issue"), `note`
  (one line, must add information — never restate the label).

## signals — the 0/1/2 disruption rubric

One score per scoreboard row: **0** within historical range; **1** statistically or
historically unusual movement but localized (one age group, one sector,
announcements not yet payrolls); **2** sustained and broad-based movement consistent
with AI-driven displacement. Composite = mean, shown once ≥6 of 8 rows are scored.
Phase 3 adds the 3-draw Claude scoring protocol; scores stay Jason's to override.

## reconciliation / bullets / bottom_line

The monthly written judgment (Page 1 bullets, bottom line, and the
"reconciling the evidence" paragraphs). `bullets` = list of strings;
`reconciliation` = list of paragraph strings. Written fresh each issue.

## issue

Label of the current issue, e.g. `"Issue 1 — August 2026"`. Shown in the hero.
