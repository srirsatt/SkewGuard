# SkewGuard development progress

## Roadmap

| Milestone | Status |
| --- | --- |
| 1: execution foundation | In progress: four-cell ORM execution and normalization implemented; ordering and CI remain |
| 2: graph and generation | Not started |
| 3: comparison, triage, reports, CLI | Reports and CLI commands implemented; CLI orchestration pending; triage remains |
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
- The completed comparison implementation passed all 162 then-existing checks
  with Docker enabled, with none skipped
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
  The completed `skewguard/exec/diff.py` also has import-order/line-length flags;
  user-authored formatting has been retained.
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
- Comparison evidence models distinguish unconfirmed divergence candidates from
  coverage/setup/new-code health issues. Candidates retain both raw outcomes and
  an explicit body comparison policy.
- Comparison helpers validate matching scenario IDs, restrict paired comparisons
  to old-code skew cells, require working baselines, recognize unavailable probe
  inputs, and serialize normalized bodies without collapsing JSON value types.
- `compare_matrix` pairs A0D1/A0D1w with A0D0, checks A1D1 execution health
  separately, and retains missing checks, empty baseline/head cells, failed
  baselines, unavailable inputs, and failed rollback setup as issues.
  Generic ORM body equality is opt-in, allowing expected additional rollback rows.
- `compare_outcomes` is implemented and all 29 comparison unit cases pass.
  The full Docker-backed run also verifies candidate classification for all three
  shop cases. An accidental runtime import from pytest was removed.
- Reproduction records preserve two numbered attempts, full replay cell evidence
  or execution errors, and per-candidate confirmed/flaky/inconclusive decisions.
- `verify_matrix` runs an initial matrix and schedules two additional fresh-state
  matrices only when there are candidates. It reuses release environments and
  seed inputs, records known replay errors without exposing passwords, and
  propagates initial setup failures or unexpected programming errors.
- Reproduction matching checks identity, policy, status, and exact exception
  metadata, plus normalized bodies when requested. Relevant missing coverage is
  distinct from a disappearing failure. Dropped/incomplete candidates are logged
  and remain in evidence; replay-only candidates are not promoted.
- `classify_reproductions` requires exactly two distinct numbered attempts,
  confirms only two matching failures, gives unavailable coverage precedence as
  inconclusive, and records disappearing/changed failures as flaky. It retains
  every original candidate in order with a reason for its decision.
- Twenty-four unit cases and a PostgreSQL integration test cover reproduction,
  including three independent matrices for the rename case. Repository-wide
  Pyright and Ruff checks on `skewguard/exec/verify.py` pass.
- The completed classifier passed the full 187-test suite with Docker/PostgreSQL
  enabled and no tests skipped or deselected. This run uses the checked-in
  implementation, without a temporary replacement classifier.
- Versioned JSON reports preserve pinned SHAs, run inputs, full initial/replay
  evidence, per-candidate decisions, and deterministic summaries. Loading checks
  that comparisons, decisions, and summaries agree with saved cell outcomes.
  Reports are portable after the original repository is removed.
- Report summaries distinguish reproduced breaks, no breaks detected in executed
  checks, and inconclusive verification. Flaky failures, coverage issues, failed
  replays, and replay-only candidates cannot silently become a clear result.
- Markdown rendering includes confirmed/flaky/inconclusive findings, pinned
  releases, issue observations, and replay errors. It escapes evidence text and
  bounds displayed details while preserving full JSON. File writes are atomic.
- Typer 0.27.3 is pinned after checking PyPI and official documentation. The
  `skewguard` entry point and `python -m skewguard` expose `run` and `report`.
  Arguments include explicit Django settings, optional seed/backend selection,
  JSON/Markdown outputs, and existing timeouts/probe limits. Run exit codes
  distinguish no detected break (0), reproduced break (1), setup/output error
  (2), and inconclusive verification (3).
- `skewguard report` validates and renders saved evidence without application or
  database execution. CLI output paths and input settings are checked before
  expensive execution. CLI orchestration remains the single unimplemented core
  in `run_verification`; its dependent tests currently fail at that placeholder.
- Report and CLI tests cover summary uncertainty, evidence consistency, JSON
  portability, Markdown escaping, atomic-write failures, argument/exit behavior,
  saved-report rendering, and resource cleanup contracts. PostgreSQL integration
  cases exercise safe/breaking Git releases and preserve the original checkout.
- Validation using a temporary completed `run_verification` passed all 227 tests
  with Docker/PostgreSQL enabled, including actual CLI setup, report output,
  safe/breaking release verdicts, and cleanup. Pyright and targeted Ruff checks
  pass. The checked-in orchestration placeholder still needs completion and its
  own passing run; report/rendering commands and supporting code are implemented.

## In progress

- PostgreSQL connection helpers, container lifecycle, and database cloning are
  implemented and verified against a running Docker engine.
- Django migration execution, read/write probes, and four-cell execution are
  implemented. Explicit ordering probes and CI remain.
- Normalization, comparison, fresh-state reproduction, and reports are implemented.
  The CLI parser/report command are implemented; `run_verification` remains.

## Execution design

- Clone PostgreSQL template databases so each cell and reproduction attempt
  starts from a known state.
- Isolate releases using separate Git checkouts, virtual environments, and
  subprocesses.
- Preserve new-code writes within the rollback cell before running old-code reads.
- Begin the fixture corpus with a nullable field addition and a field rename.

## Next

- Add explicit ordering probes.
- Complete `run_verification` and verify end-to-end CLI execution.
- Preparation/graph nodes will own worktree, environment, and container lifetimes.
- Configure CI and pre-commit checks.
- Verify and pin agent/MCP dependencies and record the selected APIs in
  `docs/api-notes.md` before adding integration code.

## Open questions

- Finalize the remaining executor interfaces.
- Use Django 5.2 for the first fixture; retain Django 4.2 execution coverage.
- Define expected-status support for intentional negative HTTP scenarios when
  scenario execution is added. Current successful baselines require HTTP 200–399.

## Initial release scope

- Retain scenario, seed, and triage agents with LangChain, LangGraph orchestration
  and validation/retry loops, checkpointing/progress, MCP, and the GitHub Action.
- Retain all four execution cells, reproduction checks, CLI/JSON/Markdown reports,
  tracing/cost budgets, and a labeled benchmark with committed measured results.
- Defer the fixer, external OSS apps, expansion to the full case corpus,
  comprehensive baseline comparisons, and single-agent ablation.
