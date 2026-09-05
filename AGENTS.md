# Session start — memory digest (MANDATORY, do this first)

1. **Before any other tool call or work, call `memory_digest`** (synapse MCP).
2. Keep its output in mind for the whole conversation — it holds pinned/important facts about the user. Treat those as current truth unless the user contradicts them.
3. If `memory_digest` is unavailable (MCP not connected), run `node .freebuff/session-digest.cjs` from a terminal and read its output — do not start work without the digest.
4. Flag stale/wrong digest facts with `memory_feedback` instead of silently ignoring them.

# Imported project memory (from zcode)

Full memory notes live in `.opencode/memory/*.md` - read one when its topic becomes relevant.

- [Morghdari build phase](morghdari-build-phase.md) — 2026-08-24 v0.1 CLOSED + retrospective RESOLVED: owner approved C-11/C-08/C-10; synced across backend_docs (4034243) and main repo (875b4f5, 67ff564); parity 36/36; all gates re-green; next = v0.2 planning on owner go
- [Morghdari backend docs](morghdari-backend-docs.md) — design docs at M:\projects\poultry\backend_docs (its own git repo); retro resolutions committed there (4034243): NULLIF everywhere, lifecycle/capacity rewritten, Tracker §7 resolved; KB site resync of these edits still parked
- [Morghdari versions review](morghdari-versions-review.md) — versions/v0.1–v1.0 curriculum drift fixed + triple-verified 2026-08-24; remaining: Tracker §6 decisions + KB resync
- [Continue until checkpoint](continue-until-checkpoint.md) — user expects autonomous runs to reach milestones without pausing; if interrupted, brief reason + immediate resume
- [Confirm, don't explain](confirm-dont-explain.md) — no unsolicited system recaps after bulk reads; when a report IS needed, keep it plain-language + decision-oriented (dense exit report → "didn't understand"); end reports with one reply-ready action

# Synapse memory (MCP)

Use the synapse MCP tools for long-term memory across sessions. The server runs as a local SQLite store (`~/.synapse/synapse.db`); tools are `memory_write`, `memory_retrieve`, `memory_digest`, `memory_feedback`.

- Call `memory_digest` once at session start (see the mandatory step at the top of this file) to load pinned/important facts the user should just know.
- Store user preferences, decisions, and durable facts with `memory_write` (use `entityKey` for single-current-value facts like `user.employer`).
- Retrieve with `memory_retrieve` when a relevant fact may exist from earlier sessions; treat qualifiers like "may be outdated" as a signal to verify.
- Report stale/wrong retrieved facts with `memory_feedback` instead of silently re-storing.
- Never try to store credentials — Synapse rejects them by design.

