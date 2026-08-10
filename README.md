# econvitals-data

The data plane for econvitals.org: chart data (JSON) and chart images (PNG), read by the site's
pages directly over raw.githubusercontent.com URLs (commit-pinned where freshness matters).
Everything here is already publicly served on econvitals.org; nothing sensitive belongs in this
repo — briefings PDFs, maclow research summaries, and all page code live in the (private)
econvitals/site repo.

Layout mirrors the site's paths one-to-one: `data/`, `img/`, `lab/<slug>/`, `tools/<slug>/`.

Writers: the chartbook release desk and slow lane on the fleet host, plus the relocated
lab/tools refresh workflows. Every writer must push with the fetch + merge --ff-only retry
pattern. History is compacted monthly (orphan-branch swap, Saturday chore).

Spec: `chartbook/REBUILD.md` (Stage B, the data plane). Created 2026-08-10.
