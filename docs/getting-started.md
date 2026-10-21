# First step: Python setup and RunConfig

This walkthrough covers the package environment and configuration model. The
executor and CLI will be built in later steps of the learning sequence.

## 1. Understand the folders

```text
SkewGuard/                  Git repository
├── .python-version         Requests Python 3.12
├── .venv/                  Local Python environment; generated and Git-ignored
├── pyproject.toml          Package metadata, dependencies, and tool settings
├── uv.lock                 Exact resolved dependency versions
├── skewguard/              Importable Python package
│   ├── __init__.py          Marks this directory as a regular Python package
│   ├── config.py            RunConfig and its validation
│   └── py.typed             Tells type checkers this package supplies types
├── examples/
│   └── run_config.py        Creates and prints a configuration
├── tests/
│   └── test_config.py       Exercises invalid input and path handling
├── docs/
│   └── getting-started.md   This walkthrough
└── PROGRESS.md              Build progress and learning checkpoints
```

The outer `SkewGuard` directory is the project. The inner lowercase `skewguard`
directory contains the Python modules people can import.

## 2. Install the environment

We use **uv** to manage Python, dependencies, and the virtual environment. The
official [uv installation guide](https://docs.astral.sh/uv/getting-started/installation/)
lists installation methods. Its macOS/Linux installer is:

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
```

If uv was installed in `~/.local/bin` and your current terminal cannot find it,
add that directory to the current shell's executable search path:

```bash
export PATH="$HOME/.local/bin:$PATH"
```

From the repository root:

```bash
uv sync --locked
uv run python --version
```

`uv sync` installs the locked packages into `.venv`, downloading the requested
Python version if needed. `uv run` selects the project's environment for a
command. See the official [project guide](https://docs.astral.sh/uv/guides/projects/).

For this project, use `uv add PACKAGE` to add a dependency and `uv add --dev PACKAGE`
for a development tool. These commands update the dependency records. You do not
need to run `pip install` separately or activate `.venv` before using `uv run`.

An optional activation is `source .venv/bin/activate`; after activation, `python`
points at this environment. Use `deactivate` to leave it. In an editor, select
`.venv/bin/python` as the Python interpreter.

`.venv/` is a folder for Python and installed packages. A `.env` file, when we add
one for the agents, will hold environment variables such as API keys. These are
different things. This configuration example needs no API keys or Docker.

## 3. Read the imports in config.py

```python
from pathlib import Path
from pydantic import BaseModel, ConfigDict, Field, field_validator
```

- `Path` comes with Python and represents filesystem paths.
- `BaseModel` gives our class Pydantic's construction and validation behavior.
- `ConfigDict` sets the model's overall validation behavior.
- `Field` adds defaults and constraints to a field.
- `field_validator` attaches our own validation function to a field.

The first import needs no installation. The second comes from the `pydantic`
dependency declared in `pyproject.toml`. See Pydantic's
[model documentation](https://docs.pydantic.dev/latest/concepts/models/) and
[field documentation](https://docs.pydantic.dev/latest/concepts/fields/).

## 4. Read RunConfig

`class RunConfig(BaseModel)` defines a class that inherits Pydantic's behavior.
Calling `RunConfig(...)` creates one configuration instance.

The required fields are `repo_path`, `base_ref`, and `head_ref`. A declaration such
as `base_ref: str` describes a string field; without a default it is required.
References must contain non-whitespace text. Git will check whether those
references actually exist during worktree preparation.

`timeout_seconds` defaults to 60 and `probe_row_limit` defaults to 10. Both must be
positive integers. For example, `gt=0` means "greater than zero." We explicitly
use `strict=True` on these two fields, so strings, fractional values, and booleans
are rejected. The future CLI parser will supply integers.

The model settings reject unknown field names, prevent ordinary reassignment
after construction, trim surrounding whitespace from string fields, and validate
defaults. Rejecting unknown fields catches typos instead of silently using a
default for the intended setting.

`resolve_repo_directory` receives the parsed `Path`, expands `~`, converts it to
an absolute path, and checks that it names an existing directory. An absolute
path keeps pointing at the same repository after a subprocess changes its
working directory. The directory does not have to be a Git repository yet;
that check belongs to the worktree layer.

## 5. Run the example

```bash
uv run python examples/run_config.py
```

The example imports `RunConfig` from our installed local package. `Path.cwd()`
means the current working directory, so run this command from the repository
root. `model_dump_json(indent=2)` prints the settings as readable JSON.

The `if __name__ == "__main__"` guard runs `main()` when you execute the file
directly, while allowing another module to import the file without printing.

Try adding `timeout_seconds=-5` to the example's `RunConfig(...)` call. Predict
whether an object will be created, then run the example and inspect the error.
Restore the valid example afterward. This is a small configuration experiment;
the milestone's core learner TODO is still the outcome normalizer.

## 6. Check your work

```bash
uv run pytest
uv run ruff check .
uv run ruff format --check .
uv run pyright
```

Pytest executes behavior checks. Ruff checks code style and formatting. Pyright
checks type usage without running the program. Pydantic validates actual input
values while the program runs.
