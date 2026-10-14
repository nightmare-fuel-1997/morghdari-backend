# ops/

Operational material that belongs to the platform but is **not** application
source. Nothing in here is imported by the running stack.

| Directory | What it holds |
|---|---|
| `qa-harness/` | The live QA drivers used against the deployed VPS stack in September 2026, plus their recorded results and the test plan they implement. |
| `vps-day-2026-09/` | The operational reports written during the VPS bring-up and hardening days (16–19 September 2026). |

## Why this is here

This material originally lived only on the production host at
`/opt/morghdari` and had no commit anywhere — a single disk was the only copy of
the evidence for what was tested and what was found. It was recovered into the
repository on 2026-10-14 during the production/local reconciliation.

The QA drivers sat in `scripts/` alongside real deploy tooling. They were moved
here so the `scripts/` directory stays readable: it now holds only the scripts
that are actually run as part of deploying or operating the platform.

## Note on the harness scripts

They are **snapshot-specific**. They drive one particular deployed stack over
HTTPS against `https://localhost/api/v1` and record verdicts into
`qa-harness/qa_results.jsonl`. They are kept as evidence and as a pattern to
copy from, not as a suite to re-run unchanged. `qa_lib.py` self-checks:

```bash
python ops/qa-harness/qa_lib.py
```
