# SkewGuard development progress

## Roadmap

| Milestone | Status |
| --- | --- |
| 1: execution foundation | In progress: configuration, execution records, and isolated checkouts complete |
| 2: graph and generation | Not started |
| 3: comparison, triage, reports, CLI | Not started |
| 4: benchmark and evaluation | Not started |
| 5: MCP and GitHub Action | Not started |
| 6: optional fixer and release documentation | Not started |

## Implemented

- Python 3.12 package layout and uv dependency lockfile.
- `RunConfig` with required repository/base/head, positive timeout and row limits,
  absolute repository paths, and rejection of unknown settings.
- Configuration example and validation tests.
- Ruff, Pyright, and pytest configuration.
- Development setup documentation.
- Execution package structure and `Cell` identifiers for all four matrix cells.
- `Outcome` evidence fields with status consistency validation.
- `CellResult` groups validated outcomes and rejects duplicate scenario IDs
  within a cell.
- Evidence tests cover JSON round trips, conflicting status fields, invalid nested
  results, and duplicate scenario IDs.
- Git command helpers provide timeouts, revision-to-commit resolution, and detached
  worktree creation/removal.
- `managed_worktrees` pins both revisions before creating separate temporary
  checkouts and removes successfully created worktrees on normal or exceptional
  exit, including when setup of the second checkout fails.
- All 60 tests pass, including 12 Git helper/lifecycle cases covering checkout
  isolation, cleanup, and preservation of original changes and unrelated worktrees.
  Ruff lint/format checks and Pyright pass.

## In progress

- The remaining execution foundation covers per-checkout environments,
  PostgreSQL snapshots, deterministic probes, and the matrix runner.

## Execution design

- Clone PostgreSQL template databases so each cell and reproduction attempt
  starts from a known state.
- Isolate releases using separate Git checkouts, virtual environments, and
  subprocesses.
- Preserve new-code writes within the rollback cell before running old-code reads.
- Begin the fixture corpus with a nullable field addition and a field rename.

## Next

- Add per-checkout virtual environments.
- Add disposable PostgreSQL containers and database snapshots.
- Build the first fixture and deterministic ORM probe matrix.
- Implement outcome normalization and executor tests.
- Configure CI and pre-commit checks.
- Verify and pin agent/MCP dependencies and record the selected APIs in
  `docs/api-notes.md` before adding integration code.

## Open questions

- Finalize the remaining executor interfaces.
- Confirm Docker availability and select the first fixture's Django version.
