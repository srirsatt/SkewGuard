# SkewGuard progress

## Learning record

| Milestone | Status | Kickoff done | Learner TODO done | Quiz score | Notes written |
| --- | --- | --- | --- | --- | --- |
| 1: execution foundation | In progress: configuration and setup | Discussion done; remaining executor skeletons pending | No; outcome normalizer reserved | Not taken | No |
| 2: graph and generation | Not started | No | No | Not taken | No |
| 3: comparison, triage, reports, CLI | Not started | No | No | Not taken | No |
| 4: benchmark and evaluation | Not started | No | No | Not taken | No |
| 5: MCP and GitHub Action | Not started | No | No | Not taken | No |
| 6: stretch work and presentation | Not started | No | No | Not taken | No |

## Milestone 1

### Agreed decisions

- Clone PostgreSQL template databases to give each cell and reproduction attempt
  a known starting state.
- Isolate releases using separate virtual environments and subprocesses.
- Start with a nullable field addition and a field rename in the first fixture.
- The learner correctly predicted separate database copies. The rollback cell
  deliberately preserves new-code writes before running old-code reads.
- The learner requested implementation and a walkthrough of `RunConfig` and
  Python setup. This step is limited to that part of the milestone.

### Completed configuration step (2026-10-21)

- Python 3.12 package layout, dependency configuration, and developer tool settings.
- `RunConfig` with required repository/base/head, positive timeout and row limits,
  an absolute existing repository directory, and rejection of unknown settings.
- A runnable configuration example, validation tests, and a setup walkthrough.
- Installed uv 0.13.4 and created `.venv` with Python 3.12.15.
- Pinned Pydantic, pytest, Ruff, and Pyright; generated `uv.lock`.
- Verified the runnable example, 19 passing tests, Ruff lint/format checks, and
  Pyright with zero errors. Commands ran through uv with `--no-cache --no-sync`
  to use the installed environment within the filesystem sandbox.
- The configuration step is complete; milestone 1 remains in progress.

### Next

- Walk through the environment, imports, and configuration with the learner.
- Sketch and approve the remaining executor contracts, including `Cell` and
  `Outcome`, before implementing the executor.
- Finish milestone scaffolding/CI; verify and pin the agent/MCP APIs in
  `docs/api-notes.md` before adding their code.
- Build worktrees, checkout environments, disposable PostgreSQL snapshots, the
  first fixture, and the deterministic matrix.
- Leave exactly one core function, `exec/diff.py::normalize_value`, for the
  learner with a contract and failing test when that module is introduced.
- Complete the learner TODO before calling the milestone complete. Then conduct
  its debrief, quiz, break-it exercise, and review the learner's notes.

### Open questions

- Remaining executor type/signature choices are not approved yet.
- Docker availability and the fixture's Django version remain to be checked.

`NOTES.md` belongs to the learner and has not been created or edited.
