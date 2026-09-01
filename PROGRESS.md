# Progress — v0.5 Provisioning + Zero-Touch

## Session start — 2026-09-01

- Inspected git status and confirmed a dirty v0.4 showcase checkout.
- Created branch `v0.5-m5`, but this initially inherited the `demo/v0.4-showcase` presentation branch rather than the v0.4 release baseline. This is now treated as a branch-baseline error requiring recovery before further implementation.
- Read the v0.5 task contract, README, tutorial, walkthrough, and relevant Django settings/models/services.
- Chosen first implementation slice: M5-01 status-transition service, because it is the prerequisite for all remaining v0.5 flows.

## M5-01 implementation

- Added `backend/apps/provisioning/services.py` with row-locking `transition()`, audit creation, post-commit cache invalidation, idempotent same-status handling, and cached status lookup.
- Added `backend/tests/test_m5_01_transition.py` covering the normal transition, idempotence, and cache population.
- Existing provisioning migrations already contain the audit table; no migration was needed for this slice.

## Validation

- File-change hooks unavailable in SDK mode.
- Targeted test command attempted: `python -m pytest tests/test_m5_01_transition.py -q`.
- Blocked because the active Python environment has no `pytest` module.

## M5-02 initial implementation

- Added `backend/apps/provisioning/api.py` with authenticated gateway request submit/list and platform review endpoints.
- Added configured pending-request cap enforcement.
- Approval creates a gateway slot, bcrypt-compatible credential, and publish ACL row; rejection records reviewer and reason.
- Added `backend/apps/provisioning/urls.py` and mounted it under `/api/v1/`.
- This is an initial slice; extensible platform permission support depends on a `PlatformPermission` model that is not yet present and currently fails closed.

## Validation

- Not rerun after M5-02 edits; the prior environment is still missing `pytest`.

## Baseline correction required

- The user clarified that v0.5 must continue from the v0.4 release implementation, not the demo showcase branch.
- Existing v0.5 edits are on disk but must not be blindly carried into a corrected baseline.

## Next

Recover from the v0.4 release baseline and then reapply the intended v0.5 changes selectively.
