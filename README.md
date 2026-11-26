# SkewGuard

An execution-based deploy-skew verifier for Django migrations.

SkewGuard is in early development. Configuration, isolated Git checkouts and
Python environments, PostgreSQL database copying, and Django migration execution
are implemented. The shop fixture, seed-data loader, ORM read/write probes,
and four-cell matrix execution are available. Outcome normalization, comparison,
and fresh-state reproduction checks are implemented. LangGraph connects static
analysis, optional HTTP scenario generation, execution verification, and reporting.

`skewguard run` prepares isolated releases and PostgreSQL, verifies migration
behavior, streams phase progress, and writes versioned JSON evidence plus Markdown.
Use `--scenarios examples/shop_http_scenarios.json` with the demo shop to run
HTTP reads and writes through the same matrix and reproduction checks.
Runs have local SQLite checkpoints; `skewguard resume RUN_ID` continues unfinished
work using pinned commits. `skewguard report`
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

To try safe and breaking releases locally, generate a disposable shop repository:

```bash
uv run python scripts/make_demo_repo.py /tmp/skewguard-demo
```

The destination must not already exist. The generated `base`, `safe`, and
`breaking` refs are described in the [CLI demo guide](docs/getting-started.md#manual-cli-demo).
With Docker and a running Ollama instance, you can ask a locally installed model
to propose one HTTP scenario for the demo shop.

```bash
uv run skewguard run --repo /tmp/skewguard-demo --base base --head breaking \
  --settings shop.settings --seed products --scenario-model qwen2.5-coder:7b
```

SkewGuard validates each proposal and tries the request on the old release.
If the proposal fails either check, the model receives
the checker’s feedback and gets one revision. The report records both attempts
when a retry occurs. If neither qualifies, the matrix continues with the ORM
probes and any supplied scenarios. Use `--max-llm-calls 1` to disable the retry.
Omit `--scenario-model` to run entirely with supplied checks and ORM probes.
