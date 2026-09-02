# rig design

## Purpose

`rig` coordinates coding agents while keeping acceptance outside agent self-report. It turns a task into
bounded units, executes each unit in an isolated Git worktree, reviews the staged diff, runs a deterministic
gate, and advances the integration base only after all required checks pass.

The design targets local, long-running work where a process may be interrupted and where a human wants a
reviewable branch rather than uncommitted changes in the main checkout.

## System shape

```text
CLI task + selected flow
          |
          v
     Phase runner
          |
          +---- read-only scout / planner / critic
          |
          +---- unit executor ---- isolated worktree ---- staged diff
          |                              |                    |
          |                              |                    +---- judge panel
          |                              |                    +---- command gate
          |                              v
          |                         unit branch
          |
          +---- reconciliation ---- session branch
          |
          v
   SQLite journal and memory
```

The package layers have narrow responsibilities:

- `rig/core` defines result, launch, event, and domain contracts.
- `rig/adapters` maps a model invocation to a supported agent CLI.
- `rig/transport` controls where and how processes and project files are accessed.
- `rig/flow` loads phase data and coordinates role-specific execution.
- `rig/checks` validates plans, aggregates independent reviews, and executes gates.
- `rig/db` stores versioned SQLite state; `rig/memory` interprets it for recall and recovery.
- `rig/tools` exposes guarded spawn, notification, and memory operations.
- `rig/entry` implements user, lead, initialization, and viewing commands.
- `flows` contains workflow JSON and role prompts as data.

## Core invariants

### Git is the artifact boundary

Workers edit an isolated worktree. The review panel sees the staged Git diff, not a worker's narrative.
When the worker, panel, and gate are green, `commit_staged()` must succeed before the unit is green. A failed
commit therefore blocks base advancement. Later units start from the last accepted unit branch. The primary
checkout remains untouched, and a session branch names the accumulated deliverable for human review.

### Acceptance fails closed

A judge response counts only when the adapter reports that the verdict schema was applied. Missing or malformed
verdicts become abstentions rather than approval. Empty review dimensions do not produce a passing panel.
Deterministic gate results distinguish PASS, FAIL, TIMEOUT, INFRA, and NO-GATE; only PASS contributes to a
green unit.

Gate argv proposed by a model is untrusted. A human may pin trusted argv in `<project>/.rig/gate.json`. Without
that file, local transport treats the proposed command as high risk and blocks it unless the caller explicitly
allows high-risk execution.

### Flow policy is data

A flow is an ordered list of phases with role and policy controls. Writer phases stay serial. Read-only roles
may use internal parallelism, such as one judge per review dimension. `on_fail`, retry limits, human checkpoints,
and base advancement are phase data rather than separate orchestrators.

Prompts follow the same model: a flow-specific prompt overrides the shared role prompt, which overrides the
built-in fallback. A missing explicitly named prompt is an error so a typo cannot silently weaken policy.

### Recovery follows repository reality

Sessions, invocations, events, decisions, work-board items, lessons, and in-flight markers live in SQLite under
`<project>/.rig/`. Startup compares recorded state with the current Git head and branch namespace. Git state wins
when durable notes disagree with the repository. Abandoned but coherent branches can be resumed; missing or
divergent state is reported instead of guessed.

### Memory has provenance and scope

Only a deterministic gate failure is automatic ground truth. Delegate failure, review rejection, timeout,
infrastructure failure, and missing gates are stored as pending examples. Human feedback is advisory. Memory
records are normalized, deduplicated, bounded, and scoped so a leaf worker cannot write into another session.

### Transport is the environment boundary

All adapter process and project I/O goes through a `Runner`. Local transport scrubs the worker environment and
blocks obvious catastrophic commands, but this is defense in depth rather than machine isolation. A VM or
container transport is required for hostile code. Provider credentials remain owned by provider CLIs and are
not copied into rig state.

## Extending rig

Add a workflow by copying `flows/_template` and composing existing roles before adding engine code. Add a new
agent family behind the adapter interface and keep execution behind `Runner`. A new durable concept requires a
schema migration, DAO operations, and recovery behavior. A new acceptance signal must preserve fail-closed
semantics and have a regression test for its failure path.

## Verification

The hermetic suite uses fake adapters for model-facing behavior and temporary real Git repositories for
worktree, staged-diff, branch, gate, and recovery behavior. Tmux integration runs only when a tmux server can
actually be created in the current environment. Opt-in live scenarios use real agent CLIs and are intentionally
outside the default test suite.

