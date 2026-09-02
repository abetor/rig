# rig

`rig` is a local orchestration engine for coding agents. It turns a task into isolated worktree
units, asks independent reviewers to inspect the real staged diff, runs deterministic gates, and
leaves verified branches for a human to merge.

[Quick start](#quick-start) | [Offline demo](#demo) | [Architecture](DESIGN.md) | [Tests](#tests) | [Contributing and agent guide](AGENTS.md) | [MIT license](LICENSE)

## Problem

An agent saying that a change is complete is not evidence that the change is correct. Long coding
runs also need durable state: a process can die, a reviewer can be unavailable, or one unit can fail
after earlier units have passed. `rig` keeps these concerns explicit and makes the repository and
gate results the source of truth.

## What rig does

- Runs Claude or Codex through a small adapter interface.
- Separates the thinking backend (`adapters/`) from the execution location (`transport/`).
- Creates one Git worktree and branch per unit and stacks later units on the last verified branch.
- Reviews the staged Git diff with an isolated, schema-constrained judge panel.
- Runs a deterministic command gate and classifies PASS, FAIL, TIMEOUT, INFRA, and NO-GATE.
- Stores sessions, invocations, events, lessons, and recovery markers in SQLite.
- Defines workflows as JSON plus prompt files under `flows/`, rather than hard-coding one domain.
- Supports a deterministic flow driver and an optional LLM lead that delegates through the same
  guarded CLI surface.

## Architecture

```text
task + flow.json
       |
       v
PhaseExecutor -> Spawner -> adapter -> transport -> isolated worktree
       |                                      |
       |                                      v
       +-> staged diff -> judge panel -> deterministic gate
                                      |
                                      v
                          verified branch + SQLite journal
```

The package is deliberately layered:

- `rig/core` contains the generic `agent(prompt) -> result` primitive and result contracts.
- `rig/adapters` builds commands for supported agent CLIs.
- `rig/transport` owns process and filesystem access.
- `rig/flow` loads and executes data-defined phases.
- `rig/checks` implements plan validation, review aggregation, and deterministic gates.
- `rig/db` and `rig/memory` provide durable state, recall, and recovery.
- `rig/tools` exposes guarded spawn, memory, and notification operations.
- `rig/entry` provides the user CLI, lead CLI, initialization, and read-only views.

## Quick start

Requirements: Python 3.10 or newer, Git, and at least one supported agent CLI already authenticated.
Use an editable installation from a Git checkout so the bundled `flows/` and `deploy/`
assets remain available. The core has no runtime Python dependencies; the viewer is optional.

```bash
python3 -m venv .venv
. .venv/bin/activate
python3 -m pip install -e .
python3 -m rig --init
python3 -m rig --path /path/to/project "add a health-check endpoint"
python3 -m rig --path /path/to/project --list
```

`--path` is mandatory except for `--init`; there is intentionally no implicit current-directory
default. To use the current project, pass `--path .` explicitly.

For a human approval checkpoint:

```bash
python3 -m rig --path . "add a health-check endpoint" --flow lead/dev-feature
python3 -m rig --path . --approve SESSION_ID
```

For the optional full-screen viewer:

```bash
python3 -m pip install -e '.[view]'
python3 -m rig --path . --view
```

## Demo

The offline acceptance test exercises the real Git worktree, staged-diff, judge, gate, stacking,
and recovery path with scripted agents, so it needs no model credentials or network access.
Install the test runner first with `python3 -m pip install 'pytest>=8'`:

```bash
PYTHONDONTWRITEBYTECODE=1 python3 -m pytest -q -p no:cacheprovider \
  tests/test_rig_acceptance.py
```

Opt-in live scenarios are in `smoke/live.py`. They invoke real agent CLIs and may consume paid model
quota. Run `python3 smoke/live.py --help` before choosing a scenario.

## Limitations

- Local transport is defense in depth, not a machine-security boundary. Run untrusted repositories
  or agents inside an isolated VM or container.
- An LLM-authored gate is treated as high risk. A human can pin trusted gate argv in
  `<project>/.rig/gate.json`; malformed or missing trusted configuration fails closed where the
  transport disallows high-risk execution.
- The LLM lead can use shell commands. Disabling direct edit tools is a workflow constraint, not
  filesystem confinement.
- Cost accounting depends on the adapter returning cost data. Spawn-count limits remain the hard
  backstop when a provider does not report cost.
- Cross-family review is configured by adapter choice; it is not automatically enforced.
- Live smoke tests require external agent CLIs, credentials, and quota and are not part of pytest.
- The public source and tests intentionally contain no Russian-language fixtures.

## Data and credential boundary

`rig` never uses the repository as a data home. Per-project state is stored in
`<project>/.rig/memory.db`, and `.rig/` is ignored. Global configuration defaults to
`$HOME/.rig/.env`; process environment variables take precedence. Important variables include
`RIG_WORKER`, `RIG_WORKER_MODEL`, `RIG_JUDGE`, `RIG_JUDGE_MODEL`, `RIG_LEAD_MODEL`, `RIG_FLOW`,
`RIG_NOTIFY`, and `RIG_CONTENT_DIR`.

Provider authentication remains owned by the provider CLI. Notification credentials are read from
the process environment or `$HOME/.rig/.env`; they are never written to project state. Do not commit
`.env` files, `.rig/`, logs, run output, transcripts, corpora, or model caches.

## Tests

Run the complete hermetic suite without bytecode or pytest cache artifacts:

```bash
python3 -m pip install 'pytest>=8'
PYTHONDONTWRITEBYTECODE=1 python3 -m pytest -q -p no:cacheprovider
```

The suite uses fake adapters for unit tests and real temporary Git repositories for acceptance
coverage. It does not require network access, model credentials, or paid inference.

## License

MIT. Copyright (c) 2026 abetor. See [LICENSE](LICENSE).
