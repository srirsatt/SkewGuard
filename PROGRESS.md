# SkewGuard development progress

## Roadmap

| Milestone | Status |
| --- | --- |
| 1: execution foundation | In progress: four-cell ORM execution and normalization implemented; ordering and CI remain |
| 2: graph and generation | Three-node graph, SQLite recovery, CLI progress, HTTP matrix execution, scenario proposal schema, and initial system prompt implemented. Model invocation and retry routing remain. |
| 3: comparison, triage, reports, CLI | Deterministic verification, reports, and CLI implemented; triage remains |
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
  expensive execution. `run_verification` prepares separate base/head environments
  within managed worktrees, verifies the matrix within managed PostgreSQL, and
  builds the pinned evidence report before checkout cleanup.
- Report and CLI tests cover summary uncertainty, evidence consistency, JSON
  portability, Markdown escaping, atomic-write failures, argument/exit behavior,
  saved-report rendering, and resource cleanup contracts. PostgreSQL integration
  cases exercise safe/breaking Git releases and preserve the original checkout.
- `run_verification` is implemented and reviewed. Pyright passes; CLI Ruff flags
  are limited to import order and line length, with authored formatting retained.
- The completed CLI implementation passed all 227 tests with Docker/PostgreSQL
  enabled, including safe/breaking CLI runs, report output, and resource cleanup.
  No tests were skipped or deselected, and no temporary orchestration replacement
  was used.
- The static-analysis package now defines validated migration identities, source
  coordinates, operation metadata, migration records, analysis issues, and a
  release inventory. Inventories require unique identities and resolved parents
  before dependents; records retain the complete top-level operation order.
  File hashes and replacement keys support later history-change handling.
  Custom operation names and inspection gaps are retained without verdict fields.
- All 17 static metadata contract tests pass. Project-wide Pyright and Ruff checks
  for the new package/tests pass. These tests cover the transport records;
  isolated Django worker coverage is recorded below.
- The isolated migration worker now loads Django's active dependency graph with
  `MigrationLoader(None)`, orders records by graph dependencies, and emits JSON
  matching the inventory schema. It captures source coordinates/file hashes,
  selected built-in model/field operation metadata, default presence, and
  replacement keys. Custom/opaque operations remain visible as analysis issues.
- Metadata inspection configures Django's connection handler with the dummy
  backend before project settings import, then overrides database settings before
  application startup. It imports project modules but does not apply migration
  operations, run data-migration functions, or evaluate callable field defaults.
  Import/startup output is redirected to stderr to preserve the JSON protocol.
- Twenty worker cases pass against separate Django 4.2.30 and 5.2.19 environments.
  Together with the schema tests, all 37 metadata checks pass without Docker.
  Coverage includes all three shop changes, cross-app dependency resolution,
  source hashes, opaque/custom operations, replacement migrations, broken graphs,
  and database-query blocking during settings import and app startup.
  Project-wide Pyright and targeted Ruff checks pass.
- `inspect_migrations` validates the checkout environment/backend and settings
  module, invokes the worker with the release interpreter and configured timeout,
  and validates its JSON as a `MigrationInventory`. Launch failures, timeouts,
  nonzero child exits, and malformed evidence become `MigrationInspectionError`.
- `MigrationAnalysis` retains both release inventories, selected additions, and
  inspection/history issues. History checks identify removed/replaced migrations,
  edited sources or metadata, and shared records without verifiable file hashes.
  Unsupported history yields explicit issues and no guessed additions.
- `compare_inventories` preserves and deduplicates identical analysis issues;
  `analyze_migrations` inspects both release environments and compares them.
  `select_new_migrations` validates both inventories, rejects unsupported history,
  and filters by full migration identity while preserving head dependency order.
  The selector receives loaded records; worker execution and JSON decoding belong
  to `inspect_migrations`. The CLI is not connected to static analysis yet.
- The completed selector passed all 64 static-analysis checks without a temporary
  replacement, including real Django 4.2.30/5.2.19 inspection and all three shop
  release comparisons. Project-wide Pyright and targeted Ruff checks pass.
- SQL extraction records distinguish collected, partial, and unavailable scripts,
  with explicit diagnostics and analysis issues. Result identities must be unique;
  collection coverage does not create compatibility verdicts.
- `_sql_worker.py` collects head's `sqlmigrate` output against a sandbox configured
  before project settings load. The pending plan must match the selected keys.
  It advances only the inspection database between migrations for later
  constraint introspection and leaves the final migration unapplied.
- SQL collection retains gaps for Python behavior, callable defaults, custom/
  wrapped operations, and failed schema advancement. Prefix preparation can
  execute migration code on the inspection copy; no compatibility conclusion is
  derived from SQL generation or its failures.
- `run_sql_worker` validates environment ownership and returned identities/order,
  enforces the configured timeout, and redacts decoded SQL/diagnostic strings.
  `collect_sql` prepares base, clones the snapshot for head's SQL worker, preserves
  metadata/SQL issues, and converts known database/migration/worker failures into
  extraction gaps. Empty selections preserve issues without database work.
- The completed orchestration passed all 104 static-analysis/SQL checks without
  a temporary replacement, including 18 real PostgreSQL cases on Django 4.2.30
  and 5.2.19. Pyright passes. The SQL module retains a line-length Ruff flag in
  authored code; formatting has been preserved.
- SQLGlot 30.31.0 is pinned for PostgreSQL SQL-literal parsing. Code-reference
  records preserve migration/model targets, source coordinates, bounded snippets,
  scopes, parsed SQL identifiers, and source-analysis gaps without verdict fields.
- Code-reference support derives targets from selected operations, scans backend
  Python source without importing it, excludes dependencies/migrations/symlinks,
  and retains parse/read failures and dynamic query gaps. Both field-rename names
  are retained; custom database-column and model-receiver binding are not inferred.
- The completed `match_field_node` passes all 50 source-scanner tests and all
  111 selected static unit checks without a temporary replacement. The matcher
  recognizes attribute, ORM keyword, and contextual string references and returns
  no match for unsupported nodes. Project-wide Pyright and Ruff for the source
  module pass; authored formatting has been retained.
- `StaticContext` groups migration analysis, SQL coverage, and separate base/head
  source scans for planning. Validation requires SQL identities/order to match
  selected additions and rejects references to unselected migrations.
- Static-context support tests cover JSON persistence, mismatched component
  identities, retained SQL/source gaps, rewritten history, fatal inspection
  errors, and release-specific reference evidence. The completed
  `collect_static_context` passes all 122 selected static unit checks without a
  temporary replacement. It shares one migration analysis across SQL collection
  and separate release scans, preserving unavailable SQL and nested issues.
  Project-wide Pyright and targeted Ruff checks pass; authored formatting is
  retained.
- Current stable LangGraph, LangChain, LangChain Core, LangSmith, SQLite
  checkpoint, and official MCP SDK dependencies are verified and pinned.
  `docs/api-notes.md` records the APIs, state/runtime separation, typed v2
  streaming, structured agent output, middleware, checkpointing, and MCP v2.
  Provider adapters are researched; selection and installation remain pending.
- `.env.example` documents provider and LangSmith key names without credentials.
  Dependency installation and API-signature inspection made no model calls or
  trace uploads. The dependency baseline passes 357 existing tests with tracing
  disabled; 35 PostgreSQL/Docker tests were explicitly disabled and skipped.
  Project-wide Pyright passes. Agent implementation remains pending.
- `graph/state.py` defines `RunState` for prepared runs: configuration, Django
  settings, pinned commit IDs, optional seed fixture, and phase outputs for static
  context, execution evidence, and the final report. Live resources remain outside
  the saved state schema. Commit fields validate SHA shape; resolution still
  belongs to worktree preparation.
- Seven graph-state tests pass using the pinned LangGraph runtime. Tests verify
  partial updates across a three-node fixture workflow, preservation of initial
  inputs and typed earlier outputs, full nested JSON round trips, and rejection
  of invalid inputs before node execution. This fixture workflow does not run
  application probes or create PostgreSQL resources. Targeted Ruff and project-wide
  Pyright pass; graph tests narrowly exempt upstream annotation diagnostics.
- `graph/runtime.py` supplies run-scoped checkout/environment references and
  sandbox database settings through `RunResources`. Resource validation checks
  recorded commit identity against state, release-to-environment bindings, and
  existing interpreter/venv/backend paths before application inspection. The
  existing context managers retain creation and cleanup ownership.
- `graph/nodes/static_analyze.py` is the first verification graph node. It obtains
  resources through `Runtime[RunResources]`, calls `collect_static_context`, and
  returns a typed partial update preserving analysis issues. Fatal inspection
  errors propagate rather than becoming empty context.
- All 18 graph state/runtime cases pass, including a real LangGraph invocation
  with a mocked application collector, missing/mismatched/stale resources,
  backend symlink escape, error propagation, and exclusion of runtime credentials
  from serialized state. These tests use no Docker or model calls. Project-wide
  Pyright and targeted Ruff checks pass.
- `graph/build.py` now compiles `static_analyze -> execute_matrix -> report`.
  Execution delegates to the existing four-cell verifier and its required fresh
  replays. Reporting delegates to the validated report builder. Nodes require the
  preceding phase's evidence and preserve failure/coverage decisions from the
  deterministic layer; errors stop the workflow without a fabricated report.
- The CLI creates initial state from pinned SHAs, supplies `RunResources` through
  runtime context, and invokes the compiled graph while its context managers own
  the prepared resources. It checks for a final report and handles fatal metadata
  inspection errors with the existing error exit code. Static context is retained
  in graph state; saved report schema 1 continues to carry execution evidence.
- The completed graph-backed CLI passes the full 423-test suite with Docker and
  PostgreSQL enabled, with no skipped or deselected tests. Coverage includes safe
  and breaking clean Git releases, reports, preserved user worktrees, and cleanup
  after successful or failed phases. Project-wide Pyright and Ruff for the CLI,
  graph package, and changed tests pass. User-authored comments are retained.
- Graph sessions now persist typed state with the SQLite checkpointer and
  synchronous checkpoint writes. Each run has a separate thread ID and a
  crash-released process lock. The store rejects unrelated SQLite files and
  unsupported store versions; serialization permits declared evidence schemas
  and framework built-ins with pickle disabled.
- `skewguard resume RUN_ID` continues pending nodes using the saved commit SHAs,
  fresh worktrees/environments, and a new PostgreSQL sandbox. Completed phases
  retain their evidence; an interrupted matrix starts again with fresh snapshots.
  Completed runs reuse their validated report without preparing application
  resources. Preparation failures before graph execution require a new run.
- Typed v2 custom/update/value streams provide phase start/completion messages
  without printing raw graph state. A separate invocation step limit bounds
  accidental graph cycles; agent retry and model-call budgets remain pending.
- `scripts/make_demo_repo.py` creates an isolated committed shop repository with
  base, safe nullable-addition, and breaking rename refs. The manual CLI guide
  covers both releases, saved reports, and checkpoint recovery without API keys.
- Recovery coverage includes separate Python processes, isolated run IDs,
  concurrent-resume rejection, lock release after process exit, retained analysis,
  fresh runtime credentials, and repository refs moving after interruption.
  Manual PostgreSQL runs produced no detected break for the safe fixture and four
  reproduced findings for the rename fixture; completed resume returned identical
  report evidence. These are development checks, not benchmark measurements.
- The checkpoint/progress implementation passes all 443 tests with Docker and
  PostgreSQL enabled, with none skipped or deselected. Project-wide Pyright and
  targeted Ruff checks pass. Existing user-authored formatting is retained.
- The HTTP scenario schema validates local request paths, method/read-write
  agreement, request bodies, optional user identifiers, and response field names.
  Non-finite request numbers are rejected to prevent a saved request from
  silently substituting null. All 60 scenario cases and 29 existing outcome
  cases pass, as does project-wide Pyright.
- `run_http_get` runs an anonymous request in the selected checkout through the
  Django test client. The worker records the status and buffered JSON body, or
  the exception raised by the view. The launcher validates the outcome and its
  request ID. Unsupported request options are rejected before execution.
- The shop walkthrough exercised both CLI releases on disposable PostgreSQL.
  The nullable addition reported no break, the rename reproduced old-code read
  and insert failures in both skew cells, and completed resume returned the
  same JSON report. Separately, the standalone HTTP example returned HTTP 200
  and the seeded Pen and Notebook products from the base checkout.
- With Docker/PostgreSQL enabled, the full suite passes all 507 tests. HTTP
  integration covers baseline success, a missing route returning 404, the
  old view failing on the renamed column, and the head view returning its
  migrated response. Project-wide Pyright and targeted Ruff checks pass.
- The shop fixture accepts JSON POSTs on `/products/` and validates product
  input before creation. The HTTP transport forwards a scenario's method and
  body and preserves the GET-only entry point. The worker serializes JSON values
  to UTF-8 bytes and rejects non-finite numbers. All six HTTP GET and POST tests
  passed with Docker enabled, including creation, rejection of invalid and
  duplicate requests, and a separate PostgreSQL query verifying the stored row.

## In progress

- PostgreSQL connection helpers, container lifecycle, and database cloning are
  implemented and verified against a running Docker engine.
- Django migration execution, read/write probes, and four-cell execution are
  implemented. Explicit ordering probes and CI remain.
- Normalization, comparison, fresh-state reproduction, and reports are implemented.
  End-to-end CLI orchestration and saved-report rendering are implemented.
- HTTP scenarios can select top-level response fields and still retain the
  entire response as evidence. Comparison and replay share the same selection
  policy. A field missing from the baseline is recorded as a coverage issue.
  Field extraction preserves explicit nulls and skips absent keys. All 11
  response-field tests pass.
- The HTTP worker accepts an optional seeded login identifier and creates a
  fresh test client per request. The shop has an optional session-enabled
  settings module and an account endpoint for authenticated GET and POST
  checks. The worker resolves the login through the checkout's user manager.
  Missing, ambiguous, or invalid user identifiers produce an input error.
  Database query failures retain their original exception type.
- HTTP qualification runs each supported proposal on a temporary clone of
  the seeded old-release database. The executor retains the observed outcome,
  returns unsupported-method feedback before dispatch, and propagates worker
  and database setup failures. A matching HTTP response qualifies when its
  status is 200–399 and it contains all selected top-level fields. Exceptions,
  unsuccessful statuses, and missing fields produce rejection feedback.
- The CLI accepts a JSON array via `--scenarios`. Qualified HTTP requests
  run alongside ORM probes in the four cells. Rollback compares old-code
  reads after new writes with an old-release control after equivalent writes.
  Replays reuse the initially qualified requests on fresh snapshots. Reports
  and SQLite checkpoints preserve scenario inputs and execution evidence.
- The shop rollback fixture reproduces an old-reader exception only after
  the new writer creates a backordered product. In the separate nullable-field
  demo, the observed HTTP 400 on the new release identifies a model validation
  failure for blank notes and leaves the rollback write setup incomplete.
  The full suite passed all 539 tests with PostgreSQL enabled. Pyright passed.
- Static metadata records, the isolated worker, parent launcher, and comparison
  support are implemented, including the addition selector. SQL records, worker,
  launcher, and orchestration are implemented. Code-reference scanning and its
  AST field matcher are implemented. The combined planning-context model,
  collector, and tests are connected to the CLI graph. Saved planning-context
  reporting and agent-planning integration remain.
- LangGraph state, run-scoped resource bindings, execution/report nodes, initial
  graph assembly, CLI wiring, SQLite recovery, and typed progress streaming are
  implemented. Agent generation/fan-out/validation loops and tracing remain.
- The scenario agent's output schema accepts one HTTP scenario or an explicit
  skip and requires a reason for either. The initial prompt grounds proposals
  in old-release routes and view code.

## Execution design

- Clone PostgreSQL template databases so each cell and reproduction attempt
  starts from a known state.
- Isolate releases using separate Git checkouts, virtual environments, and
  subprocesses.
- Preserve new-code writes within the rollback cell before running old-code reads.
- Begin the fixture corpus with a nullable field addition and a field rename.

## Next

- Add explicit ordering probes.
- Integrate static-analysis context with scenario/seed planning and reports.
- Keep worktree, environment, and container lifetimes in the session manager;
  pass fresh runtime resources to resumed nodes.
- Configure CI and pre-commit checks.
- Select and pin the model-provider adapter before implementing model calls.
- Add bounded scenario/seed generation retries and persistent model-call budgets.

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
