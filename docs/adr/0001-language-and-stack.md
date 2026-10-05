# ADR 0001: Language and stack for the agent code

Date: 2026-10-05
Decided by: Builder (owner informed) — closes PRD open decision "TypeScript or Python"
Status: Accepted

## Decision

Python 3.12 for all agent code, with:

- **LangGraph** as the graph engine: fixed-order graphs, durable checkpoints,
  native `interrupt()` for pause-for-approval and resume after restart.
- **LiteLLM** as the model gateway: every model call goes through it; tiers
  (cheap/standard/strong) map to providers via `config/route_table.yaml`, so
  changing a model never requires changing code. Named fallback per tier.
- **FastAPI** for the web app/API (approval queue, runs, clients).
- **PostgreSQL** for all state on the VPS; **SQLAlchemy Core** for app tables
  (runs, approvals, audit) so the same code runs on SQLite for local tests.
  LangGraph checkpoints: SQLite locally, Postgres (`langgraph-checkpoint-postgres`)
  in deployment.
- **Caddy + Docker Compose** on the existing Hostinger VPS, as the PRD defaults.

## Reasons

- OpenMuse failed the PRD's one-day test (ADR 0002), so the PRD's default stack
  applies; its graph-engine row offers LangGraph (Python) or Mastra (TypeScript).
- LangGraph is Python-first and is the only candidate with mature durable
  checkpointing plus first-class human-in-the-loop interrupts.
- The harness needs structured outputs, validation and LiteLLM-style routing —
  the Python model-gateway ecosystem (LiteLLM) is the most complete.
- The PRD's harness row (see ADR 0003) is satisfied by our own step-contract
  runner on top of LangGraph rather than a second agent framework.

## Consequences

- Local development needs Python via uv (uv-managed interpreter) and runs tests
  on SQLite without Docker; Docker is only required for the VPS deployment.
- Future candidates that assume TypeScript (e.g. forking Chidimma OS's Next.js
  app directly) would run as a separate UI codebase talking to this API, which
  the interface phase will decide.
