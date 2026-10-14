# QA harness — September 2026

The drivers behind `qa-test-plan.html`, run against the deployed VPS stack.

## Layout

| Files | Role |
|---|---|
| `qa-test-plan.html` | The test plan itself — every test id, its intent and its expected result. |
| `qa_lib.py` | Shared helpers, no third-party dependencies. Paces requests at 0.25 s so harness traffic stays under nginx's `api` zone limit of 5 r/s and never 429s itself. |
| `qa_wave*.py` (56) | The wave drivers, run in order. Later waves are corrections and re-runs of earlier ones — the numbering records the order they were written, not a clean partition. |
| `qa_probe*.py` (7) | Narrow one-off probes written to isolate a single failure. |
| `qa_*.sh` | Shell wrappers: bring-up, DB reset, Redis rotation, smoke, cleanup, deploy. |
| `qa_tally.py`, `qa_watch.py` | Result aggregation and live watching. |
| `qa_results.jsonl` | 1013 recorded verdicts, one `{id, verdict, observed}` per line. This is the primary evidence. |
| `qa_wave5a.err` | Captured stderr from wave 5a. |

## Running

`qa_lib.py` is the entry point; the wave scripts import it.

```bash
python qa_lib.py                    # self-check
python qa_wave1.py                  # then subsequent waves in order
python qa_tally.py                  # summarise qa_results.jsonl
```

These expect a live stack reachable at `https://localhost/api/v1` with TLS
verification disabled (`qa_lib.py` builds an unverified context). They are
snapshot-specific — treat them as evidence and as a pattern, not as a suite to
re-run unchanged against a different deployment.
