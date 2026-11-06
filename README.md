# SkewGuard

An execution-based deploy-skew verifier for Django migrations.

SkewGuard is in early development. Configuration, isolated Git checkouts and
Python environments, PostgreSQL database copying, and Django migration execution
are implemented. The shop fixture, seed-data loader, ORM read/write probes,
and four-cell matrix execution are available. Outcome normalization, comparison,
and fresh-state reproduction checks are implemented. Agents are in development.

`skewguard run` prepares isolated releases and PostgreSQL, verifies migration
behavior, and writes versioned JSON evidence plus Markdown. `skewguard report`
validates and renders saved evidence without booting the application or Docker.

See the [development setup](docs/getting-started.md) and
[project status](PROGRESS.md).

With [uv](https://docs.astral.sh/uv/getting-started/installation/) installed, run
these commands from the repository root:

```bash
uv sync --locked
uv run python examples/run_config.py
uv run skewguard --help
uv run pytest
uv run ruff check .
uv run ruff format --check .
uv run pyright
```
