# Findings — v0.5 Provisioning + Zero-Touch

## Contract discoveries

- `versions/v0.5/TASKS.md` orders M5-01 through M5-10. M5-01 is the dependency spine: every provisioning status change must use one atomic transition service.
- The v0.5 theme is the trust boundary: PENDING_APPROVAL telemetry is buffered, approval drains FIFO, rejection/suspension blocks it, and credential rotation commits only after device ACK.
- Installer sync must derive hardware identity server-side, force PENDING_APPROVAL, and return 409 + Retry-After for a same-gateway concurrent sync.
- Review permission is platform-side and extensible; default reviewers are system admin/platform support, not organization roles.

## Repository discoveries

- Current branch before branching: `demo/v0.4-showcase`.
- New branch created: `v0.5-m5`.
- The checkout already contains substantial uncommitted v0.4 demo/backend/UI work and generated runtime artifacts. These are pre-existing and must not be discarded or broadly staged.
- Django apps already include `provisioning`, `devices`, `mqtt_auth`, `telemetry`, `alerts`, `auditing`, and `system_config`; existing Redis, DRF, Celery, bcrypt, and tenant middleware conventions should be reused.
- Existing telemetry consumer and alert code explicitly defer several v0.5 surfaces, so implementation should replace those seams rather than invent parallel abstractions.
- Existing `PROGRESS.md` is the historical v0.1–v0.4 mirror and contains non-English legacy text; new visible documentation authored in this task remains English per project policy.

## Baseline risk

- Because the working tree is dirty, all edits must be narrow and tracked independently. Validation should focus on files changed for v0.5 and avoid commands that rewrite generated/runtime artifacts.
