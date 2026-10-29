# Proposed SkewGuard architecture

SkewGuard checks Django migration compatibility by executing old and new
application releases against controlled PostgreSQL database states. The MVP
interfaces share one verification workflow.

Configuration validation, execution record types, and temporary Git worktree
management are implemented. The remaining execution and agent components below
are planned.

## Verification workflow

```mermaid
flowchart TD
    A["CLI / MCP / GitHub Action"] --> B["Parse input and validate RunConfig"]
    B --> C["Prepare old/new code, Python environments, and Postgres"]
    C --> D["Inspect migrations and affected application code"]
    D --> E["Plan ORM probes, scenarios, and seed data"]
    E --> F["Agents propose scenarios and seed specifications"]
    F --> G["Schema-check proposals and validate them on the old application and schema"]
    G -->|Invalid proposal: bounded retry with error feedback| F
    G -->|Usable checks and data| H["Execute the four-cell matrix"]
    G -->|Insufficient usable checks| R["JSON and Markdown report with evidence and coverage limits"]
    H --> I["Validate records, normalize results, and compare matching checks"]
    I -->|Candidate divergences| J["Re-run candidates twice from reset snapshots"]
    I -->|No candidate divergences| R
    J -->|Reproducible findings| K["Triage agent explains execution evidence"]
    J -->|Flaky outcomes: log separately| R
    K --> L["Verify cited files and line numbers"]
    L --> R
    R --> Z["Release temporary execution resources"]
```

- LangGraph coordinates the workflow, retries, shared state, checkpoints, and
  progress streaming. LangChain agents propose structured scenarios, seed data,
  and explanations.
- Deterministic ORM probes run alongside usable generated scenarios. The first
  executor milestone uses fixture data and deterministic probes before agents
  are introduced.
- Generated seeds must insert successfully into PostgreSQL. Generated scenarios
  must pass baseline validation before contributing to compatibility comparisons.
- Setup failures, migration-application failures, and unusable baselines must be
  reported explicitly. Incomplete execution cannot establish compatibility.
- Cleanup is attempted when contexts exit, including on exceptions; the final
  cleanup node shows the normal completion path.
- Only execution establishes findings. Agent explanations and confidence do not
  replace the observed evidence or the reproduction requirement.

## Database snapshots and matrix execution

```mermaid
flowchart TD
    B["Base code: apply old migrations and insert validated seed data"] --> D0["D0: seeded old-schema snapshot"]
    D0 -->|Fresh database copy| A0D0["A0D0: run old-code checks"]
    D0 -->|Fresh database copy| M["Apply head migrations using the head checkout"]
    M --> D1["D1: migrated database snapshot"]
    D1 -->|Fresh database copy| A0D1["A0D1: run old-code checks"]
    D1 -->|Fresh database copy| A1D1["A1D1: run new-code checks"]
    D1 -->|Fresh database copy| W["Run new-code write scenarios and commit their rows"]
    W --> A0D1W["A0D1w: run old-code reads against those rows"]
```

The two code checkouts have separate Python environments. A disposable
PostgreSQL container can host the separate test databases. Each cell starts
from a controlled snapshot, and reproduction runs reset to the appropriate
starting state. Within A0D1w, new-code writes remain present for the old-code
reads. A1D1's test writes do not supply A0D1w's starting state.

## Component responsibilities

| Component | Responsibility |
| --- | --- |
| `cli.py`, `mcp_server.py`, `action.yml` | Invoke the same verification workflow from a terminal, coding agent, or pull request. |
| `config.py` | Validate run settings and execution limits. |
| `graph/` | Coordinate stages, retries, checkpointed workflow state, and progress. |
| `agents/` | Generate scenarios and seed specifications; explain verified findings. |
| `static/` | Extract migration operations and SQL, and locate affected code references. |
| `exec/` | Manage checkouts, environments, databases, probes, scenario execution, outcome normalization, and comparison. |
| `report/` | Produce machine-readable JSON and Markdown summaries. |
| `benchmark/` | Evaluate labeled cases against baselines and the single-agent ablation using code-based metrics. |

LangSmith tracing records workflow activity and benchmark experiments. Model
budgets and caching bound generation costs. Checkpointed graph state must be
paired with retained or reconstructed execution resources when resuming a run.

An optional fixer can propose migration changes and submit them to the same
execution workflow. A fix is labeled verified only after the required matrix
checks pass. Fix generation is outside the initial execution foundation.
