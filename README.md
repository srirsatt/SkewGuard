# SkewGuard

An execution-based deploy-skew verifier for Django migrations.

SkewGuard is in early development. The configuration layer validates repository
paths, release references, timeouts, and probe row limits. The migration executor
and CLI are not yet implemented.

See the [development setup](docs/getting-started.md) and
[project status](PROGRESS.md).

With [uv](https://docs.astral.sh/uv/getting-started/installation/) installed, run
these commands from the repository root:

```bash
uv sync --locked
uv run python examples/run_config.py
uv run pytest
uv run ruff check .
uv run ruff format --check .
uv run pyright
```
