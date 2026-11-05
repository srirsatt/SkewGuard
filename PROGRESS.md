# SkewGuard development progress

## Roadmap

| Milestone | Status |
| --- | --- |
| 1: execution foundation | In progress: four-cell ORM execution and normalization implemented; ordering and CI remain |
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
- The last full Docker run passed all 126 then-existing checks, with none skipped
  or deselected. Coverage includes Git, environment isolation, PostgreSQL cloning,
  Django 4.2/5.2 migration execution, shop read/write/committed-write behavior,
  and the complete four-cell matrix.
  PostgreSQL integration tests remain opt-in for development without Docker.
  Environment tests exercise real offline virtual environments and local
  fixture wheel installs: different release dependencies, explicit install
  targets, backend subdirectories, lock preservation, stale-lock refusal, and
  protection of an existing active environment. Installation tests call the
  public entry point and cover manifest precedence and unsupported layouts.
  Pyright passes. Ruff reports import-order and line-length violations in
  `skewguard/exec/db.py` and `skewguard/exec/runner.py`, import ordering in
  `skewguard/exec/matrix.py`, and line-length violations in `skewguard/exec/envs.py`.
- Opt-in PostgreSQL integration tests exercise schema/data/sequence copying,
  independent mutations, rejection of active source connections, and cleanup
  after success or caller failure. All three passed against Docker Desktop and
  the `postgres:16` image. Explicitly enabled tests fail if Docker is unavailable;
  they do not silently skip that failure.
- Migration subprocess helpers encode explicit sandbox database settings for
  stdin, validate dotted settings-module names, discard inherited Python
  selectors, and redact the sandbox password from returned logs. Seven tests
  cover these boundaries.
- `run_migrations` boots Django using the selected checkout's interpreter and
  settings, replaces the default connection before application setup, applies
  migrations, closes connections, and captures output with a subprocess timeout.
  Six committed integration cases exercise Django 4.2.30 and 5.2.19, separate
  base/head environments, repeat runs, a renamed column in a copied database,
  missing settings, and a slow migration. Fixtures intentionally configure an
  unusable original database to verify that the sandbox connection is used.
- The first benchmark fixture is a Django 5.2.19 shop with deterministic product
  records, `Meta.ordering`, and a plain JSON view using `.values()`. Three labeled
  case definitions cover a price rename, a safe nullable addition, and a required
  category column that breaks old-code inserts.
  The fixture integration test verifies seeding, both migrations, and preservation
  of baseline rows against real PostgreSQL.
- `seed_database` commits Django fixture inserts. The probe subprocess wrapper
  validates returned records as `Outcome` objects; its worker runs in the
  checkout's interpreter and closes connections before returning.
- `probe_model` reads bounded rows and every concrete field using the selected
  release's model manager, preserves default ordering, and records exception
  type/message and integrity-error evidence. The shop regression test verifies
  baseline success, an old-code read failure after the rename, new-code success,
  and compatibility of the nullable-field addition.
- Write-probe preparation includes deterministic model-driven field values,
  default/choice handling, field-level uniqueness checks, seeded foreign-key
  values, and a `run_write_probes` subprocess entry point. Unsupported generation
  is identified by `ProbeValueError` and must not become a compatibility verdict.
- `probe_model_write` attempts an insert inside an atomic transaction, rolls back
  on success or failure, and records structured execution evidence. The shop's
  required-category case preserves reads but rejects old-code inserts. The
  shop integration tests pass, covering repeated probes, the old-code integrity
  error, new-code success, and unchanged rows after rollback.
- `run_committed_writes` prepares rollback checks using an atomic batch of
  head-code inserts and preserves commit evidence. A dedicated regression test
  verifies row persistence, the new model's Python default, and failure rollback.
- `collect_cell` gathers read, rolled-back-write, and optional setup evidence as
  validated cell records. Shop integration tests now share prepared releases.
- `run_matrix` builds old and migrated snapshots and uses separate clones for
  A0D0, A0D1, A1D1, and A0D1w. Only A0D1w receives committed head-code writes
  before old-code probes. Returned cell records preserve committed-write
  evidence. The matrix test covers all three shop cases and sequence isolation.
- `normalize_value` recursively copies JSON bodies, sorts dictionary keys, and
  masks explicitly selected volatile scalar fields. It preserves keys, row order,
  row counts, nulls, booleans, and undeclared business values. `normalize_outcome`
  retains scenario IDs, statuses, HTTP codes, integrity flags, and exceptions.
  No fields are ignored by default. All seven normalization tests pass, and
  Pyright and Ruff checks for the normalization module pass.

## In progress

- PostgreSQL connection helpers, container lifecycle, and database cloning are
  implemented and verified against a running Docker engine.
- Django migration execution, read/write probes, and four-cell execution are
  implemented. Explicit ordering probes and CI remain.
- Normalization is implemented and tested independently; comparison and fresh-state
  reproduction still need to connect it to the matrix evidence.

## Execution design

- Clone PostgreSQL template databases so each cell and reproduction attempt
  starts from a known state.
- Isolate releases using separate Git checkouts, virtual environments, and
  subprocesses.
- Preserve new-code writes within the rollback cell before running old-code reads.
- Begin the fixture corpus with a nullable field addition and a field rename.

## Next

- Add explicit ordering probes.
- Add comparison and fresh-state reproduction checks before producing verdicts.
- Preparation/graph nodes will own worktree, environment, and container lifetimes.
- Configure CI and pre-commit checks.
- Verify and pin agent/MCP dependencies and record the selected APIs in
  `docs/api-notes.md` before adding integration code.

## Open questions

- Finalize the remaining executor interfaces.
- Use Django 5.2 for the first fixture; retain Django 4.2 execution coverage.
- Define comparison rules for expected additional A0D1w rows; their presence is
  not itself evidence of an incompatible migration.

## Initial release scope

- Retain scenario, seed, and triage agents with LangChain, LangGraph orchestration
  and validation/retry loops, checkpointing/progress, MCP, and the GitHub Action.
- Retain all four execution cells, reproduction checks, CLI/JSON/Markdown reports,
  tracing/cost budgets, and a labeled benchmark with committed measured results.
- Defer the fixer, external OSS apps, expansion to the full case corpus,
  comprehensive baseline comparisons, and single-agent ablation.
