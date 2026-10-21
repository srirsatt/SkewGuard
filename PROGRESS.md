# SkewGuard development progress

## Roadmap

| Milestone | Status |
| --- | --- |
| 1: execution foundation | In progress: configuration complete; execution types next |
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
- Execution package structure; shared types are pending implementation.

## Execution design

- Clone PostgreSQL template databases so each cell and reproduction attempt
  starts from a known state.
- Isolate releases using separate Git checkouts, virtual environments, and
  subprocesses.
- Preserve new-code writes within the rollback cell before running old-code reads.
- Begin the fixture corpus with a nullable field addition and a field rename.

## Next

- Define `Cell`, `Outcome`, and `CellResult` in `exec/types.py`.
- Implement worktree preparation and per-checkout environments.
- Add disposable PostgreSQL containers and database snapshots.
- Build the first fixture and deterministic ORM probe matrix.
- Implement outcome normalization and executor tests.
- Configure CI and pre-commit checks.
- Verify and pin agent/MCP dependencies and record the selected APIs in
  `docs/api-notes.md` before adding integration code.

## Open questions

- Finalize the remaining executor interfaces.
- Confirm Docker availability and select the first fixture's Django version.
