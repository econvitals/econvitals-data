# builders/ — the scheduled data builders for this repo

These are the niche lab/tools fetchers relocated from `econvitals/site` (2026-08-10,
chart-system rebuild Stage B — see `chartbook-edit/REBUILD.md` §4), plus one new one
(ai-monitor, which previously had no scheduled refresh anywhere). Each runs on a GitHub
Actions cron (`.github/workflows/<slug>.yml`), writes its output into the mirrored
`lab/<slug>/` or `tools/<slug>/` directory of THIS repo, and commits only on change.
The pages that render this data live in `econvitals/site` — no page code is here.

| Builder | Output | Schedule | Repo secret |
|:---:|:---:|:---:|:---:|
| central-bank-stance | tools/central-bank-stance/data.json | daily 07:10 UTC | none (BIS, keyless) |
| fed-dot-plot | tools/fed-dot-plot/data.json | Wed/Thu/Sun 20:30 UTC | FRED_API_KEY |
| housing-monitor | lab/housing-monitor/data.json | daily 22:00 UTC | FRED_API_KEY |
| risk-monitor | lab/risk-monitor/data.json | daily 22:30 UTC | FRED_API_KEY |
| ai-monitor | lab/ai-monitor/data.json | daily 23:00 UTC | FRED_API_KEY |
| how-americans-are-doing | lab/how-americans-are-doing/data.json | monthly, 1st 12:00 UTC | FRED_API_KEY |
| poverty-safety-net | lab/poverty-safety-net/data.json | monthly, 3rd 08:30 UTC | CENSUS_API_KEY |

Repo secrets needed (Settings → Secrets → Actions): **FRED_API_KEY**, **CENSUS_API_KEY**.
This is a PUBLIC repo — keys come only from those secrets (each script also falls back to
`~/.config/macro-dashboard/.env` for local runs); never commit a key.

**Builder inputs.** Editorial/config inputs that only the builder reads live next to the
script in `builders/<slug>/` (`housing_config.yaml`, `cb_matrix_config.yaml`, the risk
monitor's `register.yaml` + `read.md`). Data inputs that were seeded into this repo's
mirrored directories are read from there, so there is exactly one copy:
`lab/ai-monitor/manual.json` + `inhouse.json` (update recipe:
`builders/ai-monitor/manual_SOURCES.md`) and `lab/how-americans-are-doing/curated.json`.
`lab/poverty-safety-net/geo.json` is committed and only rewritten if missing.

**Push discipline (required for any writer of this repo).** Many writers push here — these
crons, the fleet host's refresh pipeline, auto-pushes — so a plain `git push` can be
rejected. Every workflow's commit step is the standard pattern: stage ONLY its own output,
skip the commit when nothing changed, then a 5-attempt push loop that rebases on the
latest main (`git pull --rebase --autostash origin main`) after each rejection and fails
loudly only if all five attempts lose the race. Copy that pattern into any new workflow;
do not "simplify" it away.

**Not here:** maclow (private sell-side content — its builder, Action and data stay in
`econvitals/site`), and built-vs-trend (its `lab/built-vs-trend/` data was copied once and
has no writer).
