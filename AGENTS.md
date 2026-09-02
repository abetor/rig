# AGENTS.md - rig

Contributor contract for agents and humans changing this repository. `rig` is a local
orchestration engine for coding agents: it runs bounded work in isolated Git worktrees,
reviews the staged diff, applies deterministic gates, and leaves verified branches for a
human to merge.

## Repository and data boundaries

The engine is generic Python under `rig/`; workflow policy and prompts are data under
`flows/`. Keep a new workflow in flow data unless it introduces a reusable engine
capability. Agent CLIs and target repositories are external processes and inputs, not
dependencies to import.

Provider credentials stay with provider CLIs. Per-project sessions, events, and memory
live below the target project's .rig directory. Global configuration is read from the
process environment and may fall back to the user's .rig environment file. Never commit
credentials, environment files, runtime state, logs, transcripts, run output, or caches;
the exclusions in `.gitignore` must not be weakened for convenience.

## Reading order

1. `README.md` - purpose, quick start, limitations, and the data boundary.
2. `DESIGN.md` - architecture, invariants, extension points, and verification model.
3. `tests/` - executable contracts, especially `tests/test_rig_acceptance.py` for the
   worktree, review, gate, stacking, and recovery path.

For a focused change, continue with `rig/core/launch.py`, `rig/checks/gate.py`,
`rig/checks/review.py`, `rig/memory/boot.py`, or `rig/tools/spawn.py`.

## Working rules

1. Acceptance comes from repository state and gates, not an agent's report. Before saying
   a change is complete, run and quote the result of:

   ```bash
   python3 -m pytest -q -p no:cacheprovider
   ```

2. Keep pytest hermetic. It may use fake adapters and temporary real Git repositories, but
   must not use the network, real credentials, or paid inference. Live checks remain
   opt-in under `smoke/live.py` and outside pytest.
3. Preserve fail-closed acceptance. Missing or malformed judge output is an abstention,
   and only a PASS gate is green. Changes to this path need a failure-direction regression
   test, not only a successful example.
4. Keep process and project I/O behind the transport abstraction. Local transport is
   defense in depth, not a security boundary for hostile code.
5. Route every write to a target project's .rig directory through the helper defined in
   `rig/db/convention.py` so runtime state remains self-ignoring.
6. A database change updates `rig/db/schema.py`, adds a compatible migration in
   `rig/db/migrate.py`, and tests recovery from an existing database.
7. Add an agent family behind the adapter interface, following `rig/adapters/claude.py`,
   and keep its process and file access behind the transport layer.
8. If the design does not cover an architectural choice, stop and present options with a
   recommendation instead of silently creating a new policy.

## Contract changes

External contracts include CLI flags and exit codes under `rig/entry`, flow JSON and role
resolution under `rig/flow/execute.py`, adapter capabilities, the SQLite schema version,
and the PASS, FAIL, TIMEOUT, INFRA, and NO-GATE result classes. A contract change requires
a test plus the matching `README.md` or `DESIGN.md` update in the same commit.

## Style

Use English in source, tests, and public documentation. Use plain hyphens and no emoji.
Prefer direct code over speculative abstractions. Commits should be meaningful steps with
short imperative subjects and no co-author trailers.
