# SkewGuard

An execution-based deploy-skew verifier for Django migrations.

SkewGuard is being built milestone by milestone. The first working piece is
`RunConfig`: validated settings for the repository, release references, timeout,
and probe row limit. The migration executor and CLI are still to come.

Start with the [Python setup and RunConfig walkthrough](docs/getting-started.md).
Track the learning checkpoints and remaining work in [PROGRESS.md](PROGRESS.md).

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
