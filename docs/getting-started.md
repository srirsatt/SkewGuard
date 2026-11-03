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
Container lifecycle orchestration and database snapshots are still in development.

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
