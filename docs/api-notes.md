# Agent and orchestration API notes

Checked on 2026-11-16 against live PyPI metadata, official documentation, and
installed Python signatures on Python 3.12. These dependencies prepare the
integration; the production verification graph, agents, tracing, and MCP
endpoints are not yet implemented. Setup does not make model calls or send traces.

## Versions

| Distribution | Pinned stable version | Source |
| --- | --- | --- |
| `langgraph` | `1.2.22` | [PyPI](https://pypi.org/project/langgraph/1.2.22/) |
| `langchain` | `1.5.0` | [PyPI](https://pypi.org/project/langchain/1.5.0/) |
| `langchain-core` | `1.8.1` | [PyPI](https://pypi.org/project/langchain-core/1.8.1/) |
| `langsmith` | `0.14.17` | [PyPI](https://pypi.org/project/langsmith/0.14.17/) |
| `langgraph-checkpoint` | `4.3.2` | [PyPI](https://pypi.org/project/langgraph-checkpoint/4.3.2/) |
| `langgraph-checkpoint-sqlite` | `3.1.3` | [PyPI](https://pypi.org/project/langgraph-checkpoint-sqlite/3.1.3/) |
| `mcp` (official SDK) | `2.3.0` | [PyPI](https://pypi.org/project/mcp/2.3.0/) |

Provider selection is pending. The researched stable adapters are
[`langchain-openai==1.8.0`](https://pypi.org/project/langchain-openai/1.8.0/)
and [`langchain-anthropic==1.7.10`](https://pypi.org/project/langchain-anthropic/1.7.10/).
Pin the selected adapter before writing provider integration code. No model ID
is selected yet; model names and generation/triage budgets will be configurable.

## Graph construction and state

Use `StateGraph` from `langgraph.graph`, add nodes/edges with explicit `START`
and `END`, and call `compile` before execution. The outer verifier will use a
Pydantic state schema containing validated inputs and evidence. Nodes return
partial dictionaries; fields without a reducer are replaced when updated.
Parallel writers require explicit reducers for shared fields. Validate generated
proposals at their boundaries rather than relying only on the graph schema.
[Graph API](https://docs.langchain.com/oss/python/langgraph/graph-api)

The installed graph API triggers strict Pyright diagnostics for missing stubs
and unbound generic types in optional hooks. The graph integration tests exempt
the missing-stub import and unknown-member diagnostic locally; the state model
and project-wide strict checking retain their normal settings.

Use `context_schema` and `Runtime[Context]` from `langgraph.runtime` for run-owned
services/resources. Saved state must remain meaningful after a process exits.
Keep live connections, container objects, and cleanup stacks out of checkpoints.
Restoring state does not restore an application's database snapshot or running
processes; resume needs resource reconstruction tied to pinned commit SHAs.
[Runtime context](https://docs.langchain.com/oss/python/langgraph/graph-api#runtime-context)

## Fan-out and subgraphs

Import `Send` from `langgraph.types`. A conditional routing function can return
`Send(node_name, task_input)` entries to create work per affected target or cell.
Use reducers and an explicit join before consuming combined results; route only
validated data to workers. Subgraphs can be compiled graphs registered as nodes
when their state channels match, or invoked through an adapter when schemas
differ. Nested `create_agent` workflows will use the latter boundary as needed.
[Send](https://docs.langchain.com/oss/python/langgraph/graph-api#send),
[subgraphs](https://docs.langchain.com/oss/python/langgraph/use-subgraphs)

## Checkpoints and interrupts

Use the separate SQLite package: `langgraph.checkpoint.sqlite.SqliteSaver` for
synchronous execution, or `langgraph.checkpoint.sqlite.aio.AsyncSqliteSaver`
with async execution. Keep the saver connection open throughout execution.
Attach it when compiling and provide `configurable.thread_id` when running.
`get_state` returns a snapshot; its `next` field identifies pending nodes.
Checkpoint serialization must round-trip the project's nested evidence models;
review the installed `JsonPlusSerializer` module allowlists when adding types.
[Checkpointers](https://docs.langchain.com/oss/python/langgraph/checkpointers)

Import `interrupt` and `Command` from `langgraph.types`. Resume human input with
`Command(resume=...)` and the same thread. The interrupted node restarts from
its beginning, so preceding side effects must be safe to repeat. Keep bounded
validation retries as graph transitions rather than an unbounded interrupt loop.
[Interrupts](https://docs.langchain.com/oss/python/langgraph/interrupts)

## Typed progress streaming

Use `stream`/`astream` with explicit `version="v2"`. Each `StreamPart` has
`type`, `ns`, and `data`; narrow on `type` before reading the payload. Start with
`updates` for node changes and `custom` for progress events. Subgraph namespaces
identify nested events. With v2 values output, `invoke`/`ainvoke` returns
`GraphOutput`; read `.value` and `.interrupts` rather than legacy dictionary
interrupt access. Stream payloads need deliberate CLI rendering because full
state can contain application evidence.
[Streaming](https://docs.langchain.com/oss/python/langgraph/streaming)

## Individual agents, models, and structured results

Use `create_agent` from `langchain.agents`, with middleware passed through
`middleware=[...]`. `AgentMiddleware` and `wrap_model_call` are exported from
`langchain.agents.middleware`. Budget middleware must count actual model
requests, including retries, with a shared guard across agents. Framework
defaults are not an execution-based verdict mechanism.
[Agents](https://docs.langchain.com/oss/python/langchain/agents),
[middleware](https://docs.langchain.com/oss/python/langchain/middleware/overview)

The inner `create_agent` state uses its supported TypedDict-based `AgentState`;
the outer verifier's Pydantic schema is separate. Pass Pydantic proposal models
through `response_format`. Read the validated `structured_response` from the
agent result. `ProviderStrategy` and `ToolStrategy` live in
`langchain.agents.structured_output`; select capabilities deliberately and bound
validation retries. Database/scenario execution must validate useful behavior.
[Structured output](https://docs.langchain.com/oss/python/langchain/structured-output)

Use `init_chat_model` from `langchain.chat_models`, supplying a configured model
and provider. Install that provider's adapter; provider-agnostic construction
does not eliminate the adapter requirement. Configure model timeouts, output
limits, and usage accounting before enabling live generation.
[Models](https://docs.langchain.com/oss/python/langchain/models),
[OpenAI adapter](https://docs.langchain.com/oss/python/integrations/chat/openai),
[Anthropic adapter](https://docs.langchain.com/oss/python/integrations/chat/anthropic)

## Tracing, evaluation, and MCP

LangSmith tracing uses the configured API key and `LANGSMITH_TRACING` setting.
Offline checks disable tracing. Dataset experiments will use SDK evaluation
with code-based evaluators against committed ground truth and reproduced
execution evidence, with model usage and wall time recorded separately.
[Tracing](https://docs.langchain.com/langsmith/observability-quickstart),
[evaluation](https://docs.langchain.com/langsmith/evaluate-llm-application)

Official MCP SDK v2 exports `MCPServer` from `mcp.server`; its `.tool()` decorator
registers type-annotated tools. The planned migration tool will delegate to the
same verification entry point as the CLI. Follow the v2 SDK documentation rather
than older `FastMCP` examples or the separate third-party `fastmcp` package.
[Official MCP SDK](https://py.sdk.modelcontextprotocol.io/)
