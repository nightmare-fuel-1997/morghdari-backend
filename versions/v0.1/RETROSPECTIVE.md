# v0.1 RETROSPECTIVE — Reality vs Documentation (handoff to owner)

> EN: Full inventory of every place the real build diverged from the v0.1
> documentation/guides, what was done about it during the build, and the
> suggested permanent resolution. Per the agreed ritual: the owner concludes,
> THEN the docs are revised, THEN (and only then) v0.2 implementation starts.
>
> فارسی: فهرست کاملِ اختلاف «داکیومنت» و «واقعیتِ ساخت»، راه‌حل موقتِ اعمال‌شده و
> پیشنهادِ حل نهایی. ترتیبِ کار: تصمیم مالک ← بازنویسی داکیومنت‌ها ← شروع نسخهٔ بعد.

> ✅ **STATUS (2026-08-24): ALL THREE DECISIONS APPROVED BY THE OWNER AND APPLIED THE
> SAME DAY** — C-11 option (a), C-08 drop REFERENCES, C-10 org_id added (parity now
> 36/36). Part 2's revision checklist has been EXECUTED across backend_docs and all
> version folders; see CHANGELOG [Unreleased] and CONSTRAINTS C-08/C-10/C-11 rows.

Status legend:
- **[SETTLED]** — already resolved doc-first during the build; nothing pending.
- **[OWNER DECISION]** — interim fix shipped, permanent choice is yours.
- **[DOC REVISION PENDING]** — mechanical doc edit queued behind your conclusion.

---

## Part 1 — Complete list of fixes applied during the build

### A. Where the documentation itself was wrong (doc defects)

**1. TimescaleDB image tag never existed — C-04 [SETTLED]**
- Docs said: `timescale/timescaledb:2-pg16`.
- Reality: no such tag exists on Docker Hub (`latest-pg16`, exact pins only).
- Fix: docs re-pinned to `2.29.2-pg16` (docs commit `db4ef95`); local compose mirrors.
- Suggestion: none left — done. Rule confirmed: exact-version pins only, upgrades are deliberate bumps.

**2. Impossible combination: compression + RLS on one hypertable — C-11 [OWNER DECISION]**
- Docs said: Compartment 3 mandates columnstore compression (90 d) + retention (180 d)
  on `saloon_telemetry`; Compartment 8 mandates tenant_isolation RLS on EVERY org-scoped table.
- Reality: TimescaleDB 2.29 refuses both on the SAME hypertable, in EITHER order:
  - enabling RLS after columnstore → `operation not supported on hypertables that have columnstore enabled`
  - enabling columnstore after RLS → `columnstore cannot be used on table with row security`
- Interim fix (security-first): **RLS ON, compression OFF, retention ON** for `saloon_telemetry`;
  `metric_hourly_aggregates` keeps RLS (it never had compression); doc annotated BLOCKED inline.
- Impact if kept: raw telemetry rows stay uncompressed until dropped at 180 d — larger disk usage,
  zero security change.
- Options: (a) keep as-is; (b) compression ON + RLS replaced by a `WITH (security_barriers)`
  view pattern (more moving parts, app must read through the view); (c) revisit when a TSDB
  release lifts the restriction.
- Recommendation: **(a)** for v0.x; revisit at scale.

**3. FK pointing at a non-unique column — C-08 [OWNER DECISION]**
- Docs said: `gateway_sensor_mapping.gateway_hardware_id REFERENCES gateway_hardware_registry(hardware_id)`.
- Reality: `hardware_id` has NO unique constraint (install history implies repeated ids), and
  PostgreSQL rejects any FK whose target isn't PK/UNIQUE. The clause is un-executable as written.
- Interim fix: plain `VARCHAR(100)` column (no DB relationship); integrity checked at app level later;
  CONSTRAINTS C-08 records it.
- Options: (a) add `UNIQUE` to `hardware_id` — forces one registry row per physical device forever,
  which breaks the installed_at/removed_at history design; (b) drop the REFERENCES clause in the doc —
  honest but loses referential intent; (c) keep app-level enforcement only.
- Recommendation: **(b)** — history semantics win; mapping correctness is enforced by provisioning code.

**4. Missing org_id on alert_acknowledgements — C-10 [OWNER DECISION]**
- Docs' own invariant (AU-RLS): every tenant-scoped table carries denormalized `org_id`.
- Reality: `alert_acknowledgements` (tenant data!) is the ONE table missing it → it cannot receive
  the tenant_isolation policy; app-role reads there are unfiltered cross-tenant.
- Interim fix: excluded from the policy list (parity rule is mechanical: policy == tables WITH org_id);
  recorded as C-10.
- Fix once approved: add `org_id UUID NOT NULL REFERENCES organizations(org_id)` to the doc table +
  AU-RLS comment; propagate to model/policy lists (parity script enforces all three sources).
- Recommendation: approve — it closes the last unprotected tenant table.

**5. Naive RLS policy template breaks on pooled connections — C-12 [SETTLED, doc-first]**
- Docs said: `USING (org_id = current_setting('app.current_org_id', true)::UUID)`.
- Reality: after a transaction-local `set_config` COMMITS, `current_setting(..., true)` returns
  EMPTY STRING (not NULL) on that session. The cast then throws
  `invalid input syntax for type uuid` on every later query from that pooled connection —
  fail-loud instead of fail-closed. Caught by our own proof tests.
- Fix (doc-first): template hardened to
  `org_id = NULLIF(current_setting('app.current_org_id', true), '')::UUID`
  in database.md §8 + initdb/02_rls.sql + tenants/0002 + telemetry/0002.
- Pending: Security Architecture §4.1 mirrors the canonical DDL and should get the same line
  during the doc sweep.

### B. Framework seams (doc is fine; Django mechanics forced an adaptation)

**6. Composite primary key — C-07 [SETTLED for now]**
- Docs: `gateway_saloon_zones PRIMARY KEY (gateway_slot_id, saloon_id)`.
- Reality: composite PKs need Django ≥ 5.2; the §2 pin is `<5.1`.
- Fix: surrogate `id` + `UniqueConstraint(gateway_slot, saloon)` — identical uniqueness semantics.
- Later: if Django is ever bumped to ≥5.2, migrate to `CompositePrimaryKey`.

**7. Extending the user table — C-09 [SETTLED, doc wording may improve]**
- Docs: `ALTER TABLE auth_user ADD COLUMN is_platform_staff ...`.
- Reality: migration-only columns don't exist under test settings' `--nomigrations` table synthesis.
- Fix: custom `AUTH_USER_MODEL = accounts.User(AbstractUser)` with the field on the model;
  doc references to `auth_user` resolve to `accounts_user`.
- Optional doc polish: reword database.md to say "custom user model" instead of raw ALTER TABLE.

**8. Column-name fidelity [SETTLED — convention going forward]**
- Django auto-names FK columns `requested_by_id`; the doc says `requested_by`.
- Fix: every user/audit FK carries explicit `db_column=` so the DB matches the doc exactly.
- Convention for ALL future tables: FK attrs named after the doc column + explicit `db_column`.

**9. Type fidelity choices [SETTLED — convention]**
- Doc `SERIAL` → explicit `AutoField` (int4), overriding the project's BigAutoField default;
  `TIMESTAMPTZ DEFAULT NOW()` → `db_default=Now()` (renders `STATEMENT_TIMESTAMP()`, equivalent
  semantics); JSONB → `JSONField`; INET → `GenericIPAddressField`.

### C. Infrastructure / operational frictions

**10. EMQX 5.5 env override crash-loop — C-05 [VERIFY DOCS COPY]**
- Setting `EMQX_AUTHENTICATION__1__BACKEND` without its `MECHANISM` pair crash-loops EMQX 5.5.
- Fix (local compose): both keys always travel together, commented C-05.
- Action for doc sweep: confirm backend_docs `docker-compose.dev.yml` carries BOTH lines
  (local copy does; verify the docs-repo source matches).

**11. Compose project-name collision — C-13 [FIX APPLIED, DOC NOTE PENDING]**
- dev and ci compose files share the directory-derived default project name AND service names
  (`postgres`, `redis`). Running the CI gate silently REPLACED the running dev containers.
- Immediate fix: run the gate isolated — `docker compose -f docker-compose.ci.yml -p morghdari_ci run --rm --build test`;
  dev restores via plain `up -d` (named volumes preserved data).
- Structural suggestion for doc sweep: add a top-level `name: morghdari_ci` to the ci compose
  (compose ≥2 supports it) so isolation is automatic, and update WALKTHROUGH §4's command.

**12. HEALTHCHECK unhealthy by design — C-06 [SETTLED — awareness item]**
- Blueprint §8 healthcheck targets `/api/v1/health/`, which first exists in v0.6.
  Backend shows `unhealthy` in v0.1–v0.5 despite serving fine. Do not "fix" by weakening it.

**13. Environment quirks — C-01/C-02/C-03 [STANDING WORKAROUNDS]**
- Python: always `backend/.venv/Scripts/python.exe` / `py -V:3.12` (system default is 3.11).
- Docker Desktop starts manually; engine ready ~5 s.
- Repo forces LF via .gitattributes; Windows editors will warn — ignore.

### D. Bugs the gates themselves caught (proof the exit-gate ritual works)

**14. Star-import hides underscore names [SETTLED]** — `from .base import *` skipped
`_database_from_url`; CI's DATABASE_URL branch NameError'd while local sqlite runs passed.
Fixed with explicit import (commit `ff8c942`).

**15. Root-owned WORKDIR broke coverage [SETTLED]** — `/app` created by WORKDIR was root-owned,
so appuser's pytest-cov couldn't write `.coverage.*`. Fixed via `install -d -o appuser /app` +
`COPY --chown` (commit `ff8c942`).

**16. Incomplete auto-generated migration dependency [SETTLED]** — telemetry/0001 lacked
`('devices','0001_initial')`; the RunSQL FK to `saloon_sensor_slots` failed only on a VIRGIN
database (old dev volume masked it). Fixed; lesson: the fresh-volume bootstrap replay is mandatory,
not optional (commit `9534795`).

**17. Two silently omitted tables [SETTLED]** — `maintenance_mutes` and `tier_quota_mapping`
were missed during transcription; a systematic db_table-vs-doc diff caught them (same commit).

**18. CI image couldn't see initdb SQL [SETTLED]** — the §8 build context packages only
`./backend`; repo-root `initdb/` is mounted read-only into the CI test service
(`MORGHDARI_INITDB_DIR=/initdb`) rather than duplicated into the image (commit `fced1f7`).

---

## Part 2 — Proposed doc/guide revisions (to apply ONLY after your conclusions)

For backend_docs + versions/v0.1, once you rule on items 2, 3, 4 above:

1. `database.md` Compartment 3: finalize the compression section per your C-11 decision
   (currently annotated BLOCKED with steps commented out).
2. `database.md` Compartment 4: apply the C-10 org_id addition to `alert_acknowledgements`
   (if approved) and the C-08 REFERENCES resolution for `gateway_sensor_mapping`.
3. `database.md` §8 + `Security Architecture §4.1`: NULLIF policy template everywhere (§8 done);
   align §4.1 wording; optionally reword the auth_user ALTER as custom-user-model guidance.
4. `docker-compose.ci.yml` (docs): add `name: morghdari_ci`; ensure dev compose carries the
   EMQX MECHANISM+BACKEND pair; WALKTHROUGH §4 command gains `-p morghdari_ci`.
5. `TUTORIAL.md`: §3.7 Dockerfile snippet gets `install -d -o appuser /app` + `COPY --chown`;
   §3.9 notes RLS-before-compression impossibility (or final C-11 state); Pitfalls table gains:
   star-import/underscore names, incomplete makemigrations deps on virgin DBs, bare-contextmanager
   GUC mistake, builders-before-SET ROLE, savepoint-wrap expected failures, `-p` project isolation,
   and "run a db_table-vs-doc diff before closing M1".
6. `TASKS.md` M1-02/M1-05 acceptance lines: reflect the final compression/RLS state and the
   parity-script requirement.
7. Repo housekeeping: refresh or retire root `HANDOFF.md` (its "NOT DONE" list is stale now);
   KB site resync after all doc edits land.

---

## Part 3 — Decision card (answer these three)

| # | Question | Recommendation |
|---|---|---|
| 1 | C-11 compression vs RLS on saloon_telemetry | Keep RLS, skip compression (current state) |
| 2 | C-08 FK to non-unique hardware_id | Drop REFERENCES in doc; enforce in app |
| 3 | C-10 add org_id to alert_acknowledgements | Approve doc-first fix |

Reply "agree with all three" (or amend individually) → docs get revised → then v0.2 planning.
