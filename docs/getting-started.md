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
| `timeout_seconds` | `int` | `60` | Strict positive integer |
| `probe_row_limit` | `int` | `10` | Strict positive integer |

Unknown fields are rejected. Instances prevent ordinary field reassignment, and
defaults are validated. Git repository and reference resolution will be checked
by the worktree layer. Timeout and row-limit enforcement belong to the executor.

Run the configuration example from the repository root:

```bash
uv run python examples/run_config.py
```

The example prints a JSON configuration using the current directory, `main`, and
`HEAD`. It does not require Docker or API keys.

## Development checks

```bash
uv run pytest
uv run ruff check .
uv run ruff format --check .
uv run pyright
```

Tests cover configuration validation and path normalization. Ruff checks lint
and formatting rules; Pyright performs static type checking.
