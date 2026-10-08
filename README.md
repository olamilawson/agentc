# Personal Agent — Foundation phase

A self-hosted, single-owner agent for a marketing strategy and brand
communications owner. It prepares work — brief evaluations, creative reviews,
strategy drafts, sponsorship replies — and nothing leaves the system without the
owner's approval. The product requirements are the PRD (see the workspace);
this repository implements **Delivery phase 1: Foundation**.

## What exists now (Foundation)

- **Run record** — every run: id, workflow, client, inputs, status (queued,
  running, waiting_for_approval, waiting_for_input, done, failed, cancelled),
  prompt/skill-library versions, token and cost totals, outputs, flags.
- **Approval gate** — the queue is the only route to a release action. An
  approval matches the exact content by hash; changed content lapses the
  approval. Release calls carry idempotency keys; uncertain outcomes are never
  retried, they are marked for the owner.
- **Harness** — per-node step contract (input/output schemas, model tier,
  allowed tools, limits), retry-once-then-fail, per-run ceilings for tokens,
  cost, tool calls and time; context assembled in a fixed order with untrusted
  material marked and instruction-like text flagged.
- **Model gateway** — LiteLLM behind a route table (`config/route_table.yaml`);
  tiers map to providers with a named fallback; changing a model never changes
  code.
- **Test graph** — intake → draft (model, standard tier) → owner approval
  (interrupt) → release. The release tool is a stub for the future Microsoft
  Graph send tool.
- **Brief evaluation graph** — receive brief → extract fields (cheap) → check
  gaps (code) → score by rubric (strong) → draft assessment (standard) → verify
  evidence (string match, then a model check; returns to scoring twice at most)
  → owner approval → file the assessment to the client folder. Built and tested
  against recorded responses; not yet run against a live provider or the
  owner-scored gate set.
- **Skill library** — versioned `skills/*/SKILL.md` files; every run records
  the library version it used; the agent never edits them.
- **Client folders** — one directory per client; runs carry an immutable client
  scope; file intake (`POST /clients/{name}/files`), PDF/Word/text parsing and
  keyword retrieval are read-class tools whose payloads are checked against the
  run's scope in code, so separation never relies on an instruction.
- **Evaluation scaffold** — `evaluation/sets/` (owner-scored items; injection
  and client-separation safety sets come before the first live workflow).

## Architecture decisions

See `docs/adr/` — 0001 language and stack (Python + LangGraph + LiteLLM +
FastAPI + Postgres), 0002 the OpenMuse one-day test verdict, 0003 the harness
and skill library.

## Local development

Requires [uv](https://docs.astral.sh/uv/) (it manages Python itself).

```bash
uv sync --extra dev          # creates .venv (Python 3.12)
uv run pytest                # recorded-response tests: no network, no cost
uv run uvicorn personal_agent.api.main:app --reload   # http://127.0.0.1:8000/queue
```

Try the gate by hand:

```bash
curl -s localhost:8000/runs -X POST -H 'content-type: application/json' \
  -d '{"workflow":"foundation-test","client":"demo","inputs":{"objective":"Reply to a sponsorship enquiry","context_notes":"They want a Q2 co-branded campaign."}}'
open http://localhost:8000/queue      # approve it there, or:
curl -s localhost:8000/runs/<run_id>/approval -X POST -H 'content-type: application/json' -d '{"decision":"approved"}'
```

Evaluate a brief (a file already uploaded to the client folder, or pasted text):

```bash
curl -s localhost:8000/clients/demo/files -F file=@brief.pdf
curl -s localhost:8000/runs -X POST -H 'content-type: application/json' \
  -d '{"workflow":"brief-evaluation","client":"demo","inputs":{"brief_path":"brief.pdf"}}'
# or: "inputs":{"brief_text":"..."}
```

With no provider keys configured the model call will fail honestly — runs that
need a model stop with a clear reason. Tests never call a provider.

## Deployment (Hostinger VPS)

`docker-compose.yml` runs Postgres (internal network only), the API and Caddy
(automatic HTTPS, single entry point). Before loading any client material,
complete `deploy/hardening.md`: firewall to SSH/HTTP/HTTPS, key-only SSH,
automatic security updates, daily encrypted off-server backups with a monthly
restore drill, and one secret per provider in the server environment.

## Next phases (PRD delivery order)

1. Brief evaluation: the graph exists; still owed are the owner-scored
   evaluation set, the injection and separation safety sets, and a live run
   measuring agreement (gate: both sets pass on the owner's real briefs).
2. Sponsorship triage: Microsoft Graph via n8n watcher, ledger, Outlook drafts,
   sends under approval.
3. Creative review and drafting: page rendering, image scoring, critique loop.
4. Learning and tools: weekly review of owner edits, skill-library diffs,
   Canva/Higgsfield via MCP, cost screens.
