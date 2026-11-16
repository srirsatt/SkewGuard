# Development setup

SkewGuard targets Python 3.12 and uses uv to manage its environment and
dependencies. The configuration layer is implemented; the executor and CLI are
in development.

## Installation

Install uv using the [official installation guide](https://docs.astral.sh/uv/getting-started/installation/).
From the repository root:

```bash
uv sync --locked
uv run python --version
```

uv creates `.venv` and installs the versions recorded in `uv.lock`, downloading
Python 3.12 if needed. Select `.venv/bin/python` as the interpreter in an editor.
See the [uv project guide](https://docs.astral.sh/uv/guides/projects/) for details.

If uv is installed in `~/.local/bin` but is not on the current shell's path:

```bash
export PATH="$HOME/.local/bin:$PATH"
```

Add runtime dependencies with `uv add PACKAGE` and development dependencies with
`uv add --dev PACKAGE`. Commit changes to both `pyproject.toml` and `uv.lock`.

## Configuration

`skewguard.config.RunConfig` validates run settings with Pydantic.

| Field | Type | Default | Validation |
| --- | --- | --- | --- |
| `repo_path` | `Path` | Required | Existing directory; home expansion and absolute path resolution |
| `base_ref` | `str` | Required | Nonblank after trimming surrounding whitespace |
| `head_ref` | `str` | Required | Nonblank after trimming surrounding whitespace |
| `backend_path` | `Path` | `.` | Relative path within each checkout; no `..` components |
| `timeout_seconds` | `int` | `60` | Strict positive integer |
| `probe_row_limit` | `int` | `10` | Strict positive integer |

Unknown fields are rejected. Instances prevent ordinary field reassignment, and
defaults are validated. The worktree layer checks Git repositories and resolves
references to commits. The uv wrapper enforces the timeout for environment
commands; the probe worker enforces the read row limit.

Use `backend_path=Path("backend")` when Django lives below the repository root,
such as alongside a TypeScript frontend. Environment preparation checks that
the selected directory exists within each checkout, including resolving symlinks.
Each release's virtual environment remains at `<checkout>/.skewguard-venv`.
Frontend dependencies are outside the Django executor's scope.

Run the configuration example from the repository root:

```bash
uv run python examples/run_config.py
```

The example prints a JSON configuration using the current directory, `main`, and
`HEAD`. It does not require Docker or API keys.

## PostgreSQL preparation

Database preparation uses Testcontainers to run a disposable PostgreSQL server
and Psycopg to connect to it. Both Python dependencies are installed by `uv sync`.
A running Docker-compatible engine is also required for container tests. On macOS,
see the [Docker Desktop installation guide](https://docs.docker.com/desktop/setup/install/mac-install/).

The container helper selects `postgres:16` and generates a password for each run.
This tag pins the major version, not an immutable image; benchmark runs will need
to record or pin the image digest. Connection settings hide passwords in their
representation and JSON output.

`timeout_seconds` applies to Docker API requests, PostgreSQL connections, and
individual SQL statements. Testcontainers manages its own readiness wait; this
is not a single deadline covering an image download and the entire startup.
`managed_postgres` starts the container, checks a SQL connection, and removes the
container when its context exits, including after partial startup failures.
`clone_database` copies an idle database into a uniquely named database on the
same server. Its default source, `template0`, creates an empty database; pass the
name of a prepared database to copy its schema and rows. Close connections to the
source before cloning. Container startup, clone isolation, and cleanup have been
verified by the PostgreSQL integration tests.

Start Docker, then run the PostgreSQL integration tests from the repository root:

```bash
docker version
SKEWGUARD_TEST_POSTGRES=1 uv run pytest tests/test_db_integration.py -x -q
```

`docker version` should show both client and server information. The tests start
temporary containers; the first run downloads the PostgreSQL image if needed.
They check schema, row, and identity-sequence copying, independence of database
changes, the idle-source requirement, and cleanup after normal and exceptional
exit. These tests are skipped by default; when explicitly enabled, an unavailable
Docker engine fails the run.

Keep `SKEWGUARD_TEST_POSTGRES=1` on the same line as the command so it reaches
pytest. Alternatively, run `export SKEWGUARD_TEST_POSTGRES=1` once in the current
shell, then run pytest separately. To include the Docker tests in the full suite:

```bash
SKEWGUARD_TEST_POSTGRES=1 uv run pytest -q
```

## Django migration runner

The runner receives an explicit settings-module name such as `shop.settings`.
It uses the selected checkout's Python and backend directory; Django belongs to
that release's dependencies and is not imported into the SkewGuard process.

Migration input helpers construct the sandbox `DATABASES` configuration and send
credentials through stdin. The bootstrap loads project settings,
replaces the database connection before `django.setup()`, and calls Django's
`migrate` command. The initial runner supports one `default` database without
database routers. Projects must supply any other settings/environment values
needed at startup. Custom startup work in `manage.py` is not executed.

`run_migrations(config, environment, database, settings_module="shop.settings")`
returns the migration log and raises `MigrationError` for a failing child process,
timeout, or launch failure. This stage prepares the schema; seed data and ORM
probes come afterward.

With Docker running, execute its integration tests using:

```bash
SKEWGUARD_TEST_POSTGRES=1 uv run pytest tests/test_runner_integration.py -q
```

The tests install pinned Django 4.2.30 and 5.2.19 releases into temporary virtual
environments, requiring access to PyPI or an existing uv cache. They exercise
base/head migrations, database-copy isolation, repeat runs, errors, and timeouts.

## Shop fixture and ORM probes

The first benchmark app lives in `benchmark/apps/shop/`. It includes product
seed data and a JSON endpoint. `benchmark/cases/shop.json` records expected
labels for a column rename, a nullable-column addition, and a required-column
addition with a Python default. The corresponding migration files are copied
into head checkouts by integration tests.

`seed_database` loads a named Django fixture into the supplied sandbox and
commits the inserted rows. `run_read_probes` launches a worker using the selected
release's Python and validates its JSON records as `Outcome` objects. The worker
bootstrap, fixture loading, and `probe_model` in `skewguard/exec/_probe_worker.py`
are implemented. The read probe observes the model's default ordering, limits
row count, reads every concrete field, and records query exceptions. Its
integration test covers baseline reads, the rename failure, and the safe nullable
addition. These outcomes are execution evidence; verdict comparison and
reproduction checks are still in development.

`run_write_probes` dispatches insert checks through the same worker.
`build_create_values` generates common scalar values, respects model defaults
and choices, avoids existing field-level unique values, and uses existing seeded
rows for required foreign keys. Unsupported input generation raises
`ProbeValueError`; this is unavailable probe input, not a compatibility verdict.
Composite/custom constraints may still require explicit factories or agent seeds.
`probe_model_write` attempts an insert inside an atomic transaction and forces a
rollback before returning success. Database errors are caught outside the atomic
block. The passing regression test covers insert results, required-column
failures, repeated probes, and unchanged product rows after rollback.

Standard write probes must roll back inserted rows. PostgreSQL sequence counters
can still advance, so reproduction attempts must use fresh database copies.
The rollback-safety matrix cell preserves deliberate new-code writes before
old-code read and rolled-back-write probes.

`run_committed_writes` provides that setup operation: it commits new-code inserts
as a batch, returns separate `orm:<label>:commit` evidence, and fails if no write
completed. A failed batch rolls back and aborts rollback-cell preparation.

```bash
SKEWGUARD_TEST_POSTGRES=1 uv run pytest tests/test_shop_probes.py -q
```

## Four-cell matrix execution

`collect_cell` in `skewguard/exec/matrix.py` groups read and rolled-back-write
outcomes into a `CellResult`, optionally including committed-write setup evidence.
`run_matrix` executes all four cells and returns their records in cell order.
Its integration test covers safe, renamed-column, and required-column cases.

The caller supplies prepared base/head environments and a running disposable
server. The matrix builds an old-schema snapshot and a migrated snapshot,
then clones a fresh database for every cell. Probes run on those copies, keeping
the snapshots unchanged even when rolled-back inserts advance sequences.
Only A0D1w receives committed head-code rows before old-code probes.

```bash
SKEWGUARD_TEST_POSTGRES=1 uv run pytest tests/test_matrix.py -q
```

Matrix results are execution records, not compatibility verdicts. A0D1w may
contain additional rows by design; body equivalence is therefore opt-in during
comparison. Reproduction is required before confirming a divergence.

## Outcome normalization

`skewguard/exec/diff.py` prepares normalized copies of body evidence while
preserving raw outcomes for reporting. Callers explicitly select volatile field
names; the default policy masks nothing. Selected numeric/text leaves become
`<volatile>`, while keys, nulls, booleans, nested structures, list order, and row
counts remain observable. A field-name policy applies at every nested dictionary.
Only select fields whose values are irrelevant to that check.

`normalize_value` recursively copies dictionaries and lists, sorts dictionary
keys, and masks only declared scalar fields. `normalize_outcome` applies it to
the body of a new Outcome. Seven passing unit cases cover declared volatility,
retained business values, missing fields, ordering, input preservation,
repeatability, and retained status/exception evidence.

```bash
uv run pytest tests/test_normalization.py -q
```

Normalization leaves collected matrix records unchanged. Comparison uses it only
when a check explicitly requests body equivalence.

## Outcome comparison

The comparison support in `skewguard/exec/diff.py` matches old-code checks by
scenario ID: A0D1 and A0D1w are compared with A0D0. `compare_outcomes` classifies
individual pairs; `compare_matrix` gathers candidates and coverage/health issues.

The comparison contract requires a successful baseline. Exceptions and HTTP
responses outside 200–399 do not establish working baseline behavior. Intentional
negative HTTP scenarios will need a separate expected-status policy. Observed
`ProbeValueError` records indicate unavailable generated input, rather than an
application regression.

Execution failures take priority over response differences: integrity errors,
other exceptions, status changes, then explicitly requested body changes. Body
comparison defaults to off, because generic ORM reads can legitimately contain
new rows in A0D1w. `ComparisonPolicy(compare_body=True, volatile_fields=...)`
enables normalized body equivalence for a particular scenario. Only select this
when equal output is part of the scenario's expected behavior. Canonical JSON
comparison preserves distinctions such as `false` versus `0`.

`compare_matrix` requires exactly one record for each cell. It retains failed
baselines, missing/unmatched checks, unavailable inputs, and new-code execution
failures as separate issues. It checks A1D1 for execution health rather than
requiring new-code responses to equal old-code responses. The current ORM-only
rollback setup requires successful `orm:<label>:commit` records before comparing
A0D1w. Empty baseline/head cells are coverage issues.

`CandidateDivergence` retains raw baseline/observed outcomes and the comparison
policy. It represents a candidate awaiting reproduction, not a verified verdict.
An empty candidate list is not a safety proof, particularly when issues remain.
Two additional executions from fresh database state are still required before
reporting a reproducible deploy-window or rollback failure.

```bash
uv run pytest tests/test_comparison.py -q
```

The unit cases need no Docker. The matrix integration test checks candidate
classification for all three shop cases.

## Fresh-state reproduction

`skewguard/exec/verify.py` implements a replay runner, evidence matching, and
classification. `classify_reproductions` assigns a decision to every original
candidate after exactly two additional attempts, retaining the candidate and a
reason for later reporting.

`verify_matrix` runs an initial matrix and compares its outcomes. If there are
candidates, it calls `run_matrix` twice more with the same releases, settings,
fixture, and comparison policies. Each call creates new old-schema/migrated
snapshots and cell databases. Python environments and the PostgreSQL server are
reused; mutated cell databases are not. One pair of additional matrices rechecks
all original candidates. With no initial candidates, no replay is scheduled.

`same_divergence` requires the same cell, check, category, policy, baseline status,
and observed failure metadata, including exact exception type/message. Body
evidence is compared only under the original normalization policy when body
equivalence was requested. Changing exception messages conservatively prevent
confirmation, even if they describe similar underlying errors.

`ReproductionAttempt` retains complete cell records and comparison results, or
a known execution/setup error. `reproduction_state` distinguishes a matching
failure, a disappearing/changed failure, and unavailable coverage. Initial setup
errors propagate; known replay failures are recorded and password-redacted.
Unexpected programming errors propagate rather than becoming migration results.

The classification contract requires both additional failures to match before
using `confirmed`. Missing coverage or failed replay setup produces `inconclusive`;
otherwise a changed/disappearing failure produces `flaky`. These decisions are
per original candidate. Failures first seen during a replay are retained in its
evidence but are not promoted without their own reproduction cycle.
`VerificationResult` retains all initial/replay evidence and decisions. Dropped
or incomplete candidates are logged and preserved for later reporting. There is
no overall safety flag; comparison issues still need to be reported.

```bash
uv run pytest tests/test_verification.py -q
SKEWGUARD_TEST_POSTGRES=1 uv run pytest tests/test_verification_integration.py -q
```

## Static migration metadata

`skewguard/static/types.py` defines migration identities, source coordinates,
operation records, history/inspection issues, and dependency-ordered inventories.
This metadata is planning context, not a compatibility verdict.

`skewguard/static/_migration_worker.py` runs with a target release's interpreter
and dependencies. It uses only standard-library imports until loading that
release's Django; SkewGuard and Pydantic need not be installed in the target
environment. Its JSON output is compatible with `MigrationInventory`.

The worker loads the graph with `MigrationLoader(None)` and follows dependency
edges rather than filename ordering. It records real migration-class source
coordinates, file SHA-256 hashes, resolved parents, and declared operation order.
Since the loader has no database connection, its graph treats migrations as
unapplied and may resolve replacement nodes. Replacement keys are retained with
an explicit history issue; this graph is not an applied-migration plan.

Selected built-in field/model operations include rename, add/remove/alter field,
create/delete/rename model, model options, and table-name changes. Field metadata
includes selected flags and Python/database default presence. Callable defaults
are recorded without being called. Custom operations, `RunPython`, `RunSQL`, and
other opaque operations remain visible with `unsupported_operation` issues.
Nested operation bodies and SQL/constraint/index internals are not extracted yet.
Broken dependency graphs and conflicting leaves fail inspection.

Inspection imports project settings, app startup hooks, and migration modules.
Before settings import, the worker configures Django's connection handler with
the dummy backend; it also replaces database settings before `django.setup()`.
Startup work requiring Django database queries fails inspection. This is not a
general sandbox for arbitrary Python import side effects. Migration operation
bodies and data-migration functions are not executed by the inspector.
Module/startup output goes to stderr; stdout contains the JSON inventory.

`inspect_migrations` in `skewguard/static/migrations.py` launches the worker with
the selected release's interpreter, backend, and checkout paths. It validates
environment ownership, applies the configured timeout, and validates the returned
JSON as `MigrationInventory`. Subprocess/decoding failures raise
`MigrationInspectionError`, distinct from an execution-based compatibility finding.

`analyze_migrations` inspects both releases. Its comparison support preserves the
inventories and inspection issues in `MigrationAnalysis`. Shared migration keys
need matching file hashes and declared metadata for an ordinary append-only
comparison. Removed, edited, squashed/replaced, or unverifiable shared history
is reported explicitly, with no guessed additions. Identical issues are
deduplicated; an empty addition list accompanied by issues is not a safety claim.

`select_new_migrations` validates both inventories, requires unchanged append-only
history, and selects full `(app_label, name)` identities absent from base. It
preserves head dependency order without mutating inputs. The CLI does not yet
invoke static analysis.

Worker and parent-launcher tests use temporary Django 4.2.30/5.2.19 environments;
release-comparison tests use all three shop cases. They require uv and dependency
downloads/cache, but no Docker:

```bash
uv run pytest tests/test_static_types.py tests/test_migration_worker.py -q
uv run pytest tests/test_static_migrations.py tests/test_static_migrations_integration.py -q
```

The loader behavior is checked against Django's
[migration loader source](https://github.com/django/django/blob/stable/5.2.x/django/db/migrations/loader.py)
and [operation documentation](https://docs.djangoproject.com/en/5.2/ref/migration-operations/).

## SQL extraction context

`MigrationSQL` and `SQLExtraction` in `skewguard/static/types.py` describe SQL
coverage separately from compatibility findings. Each migration has `collected`,
`partial`, or `unavailable` SQL. Partial/unavailable records require a diagnostic;
an empty SQL string remains valid for a no-op. Repeated migration identities are
rejected. SQL text is kept as a full script, without splitting on semicolons.

`skewguard/static/_sql_worker.py` boots head's Django against an explicit sandbox
connection, configured before project settings load. It verifies that the actual
pending migration plan matches the selected keys, then uses Django's `sqlmigrate`
command to collect SQL. PostgreSQL is required. It closes connections and keeps
application output on stderr so stdout remains a JSON protocol.

For multiple pending migrations, SQL for later operations may need constraints
created by earlier ones. The worker applies each earlier migration to the
inspection copy before collecting the next migration's SQL. The final migration
is not applied. The base snapshot must be separate and remains unchanged.
Prefix advancement executes migration code, including data migrations, inside
the inspection sandbox. A failed prefix blocks later SQL collection with an
explicit schema-context issue.

Top-level Python operations are marked partial because their behavior is absent
from SQL. Callable defaults can be evaluated during SQL generation and are also
marked partial, since reevaluation may produce different values. Custom top-level
operations and `SeparateDatabaseAndState` SQL hooks are not collected by this
first extractor; their migration may still be applied when advancing an earlier
prefix. This is not a general sandbox for arbitrary Python side effects.

`run_sql_worker` in `skewguard/static/sql.py` validates the release environment,
passes database credentials through stdin, applies the configured timeout, and
checks result identities/order. It redacts decoded SQL/diagnostic strings so JSON
escaping cannot hide an exact password match. Worker startup/protocol failures
raise `SQLExtractionError`.

`collect_sql` validates the analysis, skips database work when there are no
selected additions, prepares a base-schema snapshot, and runs SQL collection on
a fresh clone. It preserves metadata issues and converts known database,
migration, and worker failures into unavailable SQL records. The caller owns
the PostgreSQL container lifetime.

These results provide planning context. A collected SQL script or an extraction
error is not a deploy-window/rollback verdict; compatibility still requires the
execution matrix and reproduction. SQL extraction is not connected to the CLI
or saved verification reports yet.

```bash
uv run pytest tests/test_sql.py -q
SKEWGUARD_TEST_POSTGRES=1 uv run pytest tests/test_sql_integration.py -q
```

The PostgreSQL cases cover both Django 4.2.30 and 5.2.19, the three shop changes,
schema advancement for constraint-name resolution, base preservation, callable
defaults, opaque operations, and failed/mismatched schema contexts.

## Application source references

`skewguard/static/code_refs.py` scans a checkout's backend using Python's AST,
without importing application modules or requiring that checkout's dependencies.
`reference_targets` groups selected migration operations by migration and model,
retaining both names of a renamed field/model. The same analysis can be used to
scan the base and head checkouts separately with `scan_code_references`.

The matcher supports attribute access, ORM keyword arguments, ORM field strings,
`Meta.ordering`, and serializer `Meta.fields`. References retain the migration
target, repository-relative file/line, enclosing class/function, and a bounded
statement snippet. Source-reading and parsing failures remain analysis issues.

Raw query literals retain their original text. Pinned
[SQLGlot 30.31.0](https://pypi.org/project/sqlglot/30.31.0/) parses them with
`read="postgres"`; `find_all(exp.Column)` and `find_all(exp.Table)` collect
identifiers following the [official metadata API](https://sqlglot.com/sqlglot.html#metadata).
Parser failures and opaque commands remain visible. Nonliteral SQL arguments,
dynamic ORM field arguments, and expanded ORM keyword arguments produce gaps;
the scanner does not execute or propagate Python values to resolve them.

Dependency directories and migrations are excluded, including custom migration
paths identified by the inventories. Symlinked source is excluded with a gap.
These are name-based candidates: receiver types, import aliases, custom
`db_column` mappings, table ownership, and wildcard SQL columns are not resolved.
An ordinary label string is ignored by the field matcher. A SQL-shaped assigned
literal can be retained even when its use cannot be established. SQLGlot parsing
does not establish that PostgreSQL would accept the query. References do not
create compatibility verdicts, and this API is not connected to the CLI yet.

```bash
uv run pytest tests/test_code_refs.py -q
```

## Combined static context

`StaticContext` groups migration metadata, SQL coverage, and separate base/head
source scans. Its validation requires SQL results to cover selected additions in
order and rejects code references to unselected migrations. Nested issues retain
their stage and release context; missing SQL can coexist with useful references.
The model contains planning observations, without compatibility verdict fields.

`collect_static_context` in `skewguard/static/context.py` is the assembly
entry point. It uses the existing metadata/SQL/source helpers and receives
caller-owned release environments and a disposable PostgreSQL server. Fatal
metadata inspection failures propagate instead of producing empty context.
The CLI and saved execution reports do not include this context yet.

```bash
uv run pytest tests/test_static_context.py -q
```

## Workflow state

`skewguard/graph/state.py` defines a Pydantic `RunState` containing run inputs,
pinned base/head SHAs, and optional static context, verification evidence, and
report outputs. Initial state is constructed after release preparation resolves
the Git refs. Output fields start as `None` until their producing phase completes.
Commit validation checks SHA format; worktree preparation resolves actual refs.

Nodes return partial updates for the fields they produce. Other fields remain
available to later nodes. Direct state attribute reassignment is disabled; live
database connections and container objects belong to runtime resources outside
saved state. A JSON round trip preserves nested evidence types, but does not
restore application resources. Checkpoint persistence and production graph/CLI
integration remain pending.

```bash
LANGSMITH_TRACING=false uv run pytest tests/test_graph_state.py -q
```

## JSON and Markdown reports

`skewguard/report/json_report.py` defines schema version `1`, pinned release
metadata, the full verification evidence, and a deterministic summary. Reports
retain initial cell records, both replay attempts, original candidate decisions,
coverage/health issues, and replay-only candidates. Counters describe recorded
evidence, not benchmark performance.

Loading JSON recomputes comparisons from saved cell outcomes, decisions from
saved replay evidence, and the summary. Conflicting labels, unsupported schema
versions, and invalid records are rejected. These consistency checks do not
authenticate the origin of a report or execute its checks again. Reading saved
evidence requires neither Docker nor the original repository.

The summary distinguishes `break_detected`, `no_break_detected`, and
`inconclusive`, with separate deploy-window/rollback results. A reproduced break
remains a finding even if other checks have issues. Without a reproduced break,
flaky/incomplete checks, issue observations, or replay-only failures prevent a
clear result. `no_break_detected` applies only to the checks that ran.

`skewguard/report/markdown.py` renders the pinned commits, results, findings,
coverage issues, and replay execution errors. Evidence text is escaped for
Markdown/HTML. Displayed outcome details are bounded; full evidence stays in
JSON. Output files are replaced atomically, so a failed individual write does
not leave a partially written report.

## Command-line interface

The CLI uses pinned [Typer 0.27.3](https://pypi.org/project/typer/0.27.3/),
`Annotated` options, and `typer.testing.CliRunner` for tests, following the
[official Typer documentation](https://typer.tiangolo.com/tutorial/options/name/).
After changing dependencies or entry points, run `uv sync --locked`.

```bash
uv run skewguard --help
uv run skewguard run --help
uv run python -m skewguard --help
```

`run_verification` in `skewguard/cli.py` owns the temporary worktrees,
per-release environments, and disposable PostgreSQL lifecycle. It runs the
verification matrix and builds a report with pinned commit IDs. The command
writes JSON/Markdown after those temporary resources are removed.

To run the in-repository shop fixture, start Docker and run from the SkewGuard
repository root:

```bash
uv run skewguard run \
  --repo . --base main --head HEAD \
  --backend benchmark/apps/shop \
  --settings shop.settings --seed products \
  --json /tmp/skewguard-shop.json
```

When both refs contain the same shop schema, this is a passing smoke check. The
CLI integration tests create separate safe/breaking fixture releases to exercise
actual migration skew.

For an existing Django repository:

```bash
uv run skewguard run \
  --repo /path/to/django-repo \
  --base main \
  --head HEAD \
  --settings project.settings \
  --seed products \
  --json reports/skewguard.json
```

Use committed releases: worktrees do not include uncommitted changes. Supply
`--settings` or set `DJANGO_SETTINGS_MODULE` to a module available in both
releases. `--seed` is optional and names a Django fixture in the base release.
When Django is below the repository root, add `--backend backend`. The existing
executor supports a backend `uv.lock` plus `pyproject.toml`, or `requirements.txt`.

The default JSON output is `skewguard-report.json`; Markdown defaults to the same
stem with `.md`, or can be selected with `--markdown PATH`. Destinations must be
different. `--timeout` and `--probe-row-limit` configure the existing limits.
Execution uses temporary worktrees/environments and a disposable PostgreSQL
container. The reports persist after those resources are removed.

| `run` exit code | Meaning |
| --- | --- |
| `0` | No break detected in executed checks |
| `1` | At least one reproducible break detected |
| `2` | Invalid input, setup/execution error, or failed report output |
| `3` | Inconclusive verification without a confirmed break |

An initial setup failure aborts without a new report. Failed additional replays
are retained as inconclusive evidence in the report. Reports may contain errors
or data produced by the target application. Current checks are deterministic ORM
probes; generated scenarios, interactive triage, and `skewguard bench` remain
planned features.

Re-render a completed report without booting the application:

```bash
uv run skewguard report reports/skewguard.json
uv run skewguard report reports/skewguard.json --markdown reports/comment.md
```

The `report` command exits `0` after successful rendering, even when the saved
evidence contains a reproduced break; invalid input or output errors exit `2`.
It refuses to overwrite its JSON input with Markdown.

```bash
uv run pytest tests/test_reports.py tests/test_cli.py -q
SKEWGUARD_TEST_POSTGRES=1 uv run pytest tests/test_cli_integration.py -q
```

## Development checks

```bash
uv run pytest
uv run ruff check .
uv run ruff format --check .
uv run pyright
```

Tests cover configuration, execution records, worktree lifecycle, and Python
environment isolation. Dependency installation tests use local fixture wheels
with network access disabled. Ruff checks lint and formatting rules; Pyright
performs static type checking.
