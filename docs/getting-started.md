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
commands; row-limit enforcement belongs to the future probe runner.

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

## Shop fixture and read probes

The first benchmark app lives in `benchmark/apps/shop/`. It includes product
seed data and a JSON endpoint. `benchmark/cases/shop.json` records expected
labels for a column rename and a nullable-column addition; the corresponding
migration files are copied into head checkouts by integration tests.

`seed_database` loads a named Django fixture into the supplied sandbox and
commits the inserted rows. `run_read_probes` launches a worker using the selected
release's Python and validates its JSON records as `Outcome` objects. The worker
bootstrap and fixture loading are implemented; `probe_model` in
`skewguard/exec/_probe_worker.py` remains unimplemented, with a failing test
covering baseline reads, the rename failure, and the safe nullable addition.

```bash
SKEWGUARD_TEST_POSTGRES=1 uv run pytest tests/test_shop_probes.py -q
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
