# superpowers-plans/

Execution plans written in the "superpowers" agent workflow during September
2026. They are the *why* behind several releases — what was broken, what the
plan was, and (in the status banners) what actually happened.

| File | Covers |
|---|---|
| `2026-09-14-fix-fail-na.md` | Driving the QA suite from 5 FAIL + 11 NA to 296 PASS / 0 FAIL / 1 NA. |
| `2026-09-15-fix-open-bugs.md` | Closing BUG-1,2,4,6,9/9b,10/18,11,12,13,21 with mirrored TDD fixes. |
| `2026-09-15-vps-deploy.md` | The `vps-02-rc1` VPS bring-up runbook (human-run VPS steps, laptop build steps). |

## Provenance

These three files existed **only** on the production host, under
`docs/superpowers/plans/`. That directory is git-ignored on purpose — it is the
superpowers agent's scratch workspace and is never committed. The plans
themselves, however, are load-bearing decision records, so on 2026-10-14 they
were copied out of the ignored workspace into this tracked directory (line
endings normalised to LF). The originals were left in place on the host.

The plans reference the old two-repo layout (`M:\projects\poultry_the_main_by_ai`
plus the sibling `M:\projects\poultry_vps_deploy`). That split is historical —
both trees were reconciled onto a single `main` on 2026-10-14 — but the plans
are kept verbatim as the record of how those milestones were reached.
