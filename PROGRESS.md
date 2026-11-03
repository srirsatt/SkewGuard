# SkewGuard development progress

## Roadmap

| Milestone | Status |
| --- | --- |
| 1: execution foundation | In progress: configuration, execution records, checkouts, and dependency installation complete |
| 2: graph and generation | Not started |
| 3: comparison, triage, reports, CLI | Not started |
| 4: benchmark and evaluation | Not started |
| 5: MCP and GitHub Action | Not started |
| 6: optional fixer and release documentation | Not started |

## Implemented

- Python 3.12 package layout and uv dependency lockfile.
- `RunConfig` with required repository/base/head, positive timeout and row limits,
  absolute repository paths, a relative backend path, and rejection of unknown
  settings. Backend paths cannot be absolute or contain parent traversal.
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
- `PythonEnvironment` records checkout, virtual environment, and interpreter
  paths. The uv command wrapper captures output, applies the configured timeout,
  and reports command, timeout, and executable errors.
- `create_environment` creates an empty Python 3.12 virtual environment inside a
  checkout, rejects invalid checkouts and existing targets, and verifies that uv
  produced a Python executable.
- Backend directory resolution checks each checkout and rejects missing folders
  or symlinks escaping the checkout. Environment validation checks the expected
  install target before dependency installation.
- The uv wrapper clears inherited environment and interpreter selectors and
  accepts explicit child-process overrides without changing the parent process.
- `install_dependencies` selects `uv.lock` with `pyproject.toml`, otherwise
  `requirements.txt`, installs into the checkout's environment, and checks
  installed dependency compatibility. A stale lock fails without fallback or
  lockfile rewriting; unsupported manifest layouts produce a setup error.
- Testcontainers 4.15.0 and Psycopg 3.3.7 are pinned. The PostgreSQL helper uses
  the current `testcontainers.community.postgres` import.
- `DatabaseConnection` records a server/database address and redacts its password.
  Helpers configure a disposable PostgreSQL 16 container and open SQL connections
  with explicit credentials, connection timeouts, and statement timeouts.
- `managed_postgres` starts a disposable server, checks it with `SELECT 1`, and
  yields connection settings. Cleanup is registered before startup; setup errors
  preserve their cause, and caller errors propagate without being reclassified.
- `clone_database` creates a uniquely named database from `template0` or a named
  source using the admin database, autocommit, and quoted SQL identifiers. It
  returns new connection settings and preserves the cause of PostgreSQL errors.
- All 105 tests pass, including 12 Git helper/lifecycle cases, 24 environment
  cases, and 15 database helper/lifecycle/cloning cases. Three additional Docker
  integration tests are skipped unless `SKEWGUARD_TEST_POSTGRES=1` is set. The
  passing database tests use mocks; real PostgreSQL execution remains unverified.
  Environment tests exercise real offline virtual environments and local
  fixture wheel installs: different release dependencies, explicit install
  targets, backend subdirectories, lock preservation, stale-lock refusal, and
  protection of an existing active environment. Installation tests call the
  public entry point and cover manifest precedence and unsupported layouts.
  Pyright passes. Ruff reports import-order and line-length violations in
  `skewguard/exec/db.py` and line-length violations in `skewguard/exec/envs.py`.
- Opt-in PostgreSQL integration tests exercise schema/data/sequence copying,
  independent mutations, rejection of active source connections, and cleanup
  after success or caller failure. Explicitly enabled tests fail if Docker is
  unavailable; they do not silently skip that failure.

## In progress

- PostgreSQL connection helpers, container lifecycle, and database cloning are
  implemented. Live startup, schema/data copying, and clone isolation validation
  remain to be completed against a running Docker engine.
- The remaining execution foundation also covers PostgreSQL snapshots,
  deterministic probes, and the matrix runner.

## Execution design

- Clone PostgreSQL template databases so each cell and reproduction attempt
  starts from a known state.
- Isolate releases using separate Git checkouts, virtual environments, and
  subprocesses.
- Preserve new-code writes within the rollback cell before running old-code reads.
- Begin the fixture corpus with a nullable field addition and a field rename.

## Next

- Validate disposable PostgreSQL containers and database clone isolation.
- Build the first fixture and deterministic ORM probe matrix.
- Implement outcome normalization and executor tests.
- Configure CI and pre-commit checks.
- Verify and pin agent/MCP dependencies and record the selected APIs in
  `docs/api-notes.md` before adding integration code.

## Open questions

- Finalize the remaining executor interfaces.
- A Docker daemon check returned connection refused, and the Docker CLI is not
  on PATH. Start or install a Docker-compatible engine before live container tests.
- Select the first fixture's Django version.
