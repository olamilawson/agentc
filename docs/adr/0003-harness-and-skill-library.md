# ADR 0003: Harness implementation, Strands evaluation, and the skill library

Date: 2026-10-05
Decided by: Builder
Status: Accepted

## Harness: our own step-contract runner on LangGraph

The PRD's harness is "the code around the model that keeps each step of a
workflow bounded and inspectable": per-node input/output schemas with
retry-once-then-fail, a model tier per node, an explicit allowed-tools list,
limits (tokens, tool calls, retries, seconds), per-run ceilings, checkpoint
after each node, and full tracing. Our workflow nodes make bounded single calls;
they are not free-running agent loops.

Evaluated: strands-agents/harness-sdk (Apache-2.0, Python+TS). It is a strong
library — agent loop with budgets, structured output, MCP client, hooks,
guardrails, evals SDK, and durable interrupts with session persistence. Deferred
for two reasons: (1) its interrupt/session capability duplicates what LangGraph
gives us natively (interrupt + Postgres checkpointer), and running two agent
runtimes doubles abstraction for no gain at this scope; (2) our nodes are
bounded single calls, so the loop-centric harness is not the shape we need.
Revisit if we later want its evals SDK or guardrails.

The step contract therefore lives in `src/personal_agent/harness/contract.py`
and is enforced by `harness/runner.py`: validate input → build context (fixed
order, untrusted data marked and instruction-like text flagged) → model call via
LiteLLM with the node's tier → validate output against the declared schema →
retry once with the validation error appended → fail the run with the reason.
Budgets are enforced per run from `config/budgets.yaml`.

## Tool gateway

`src/personal_agent/gateway/` implements the PRD's three tool classes.
Read tools fetch; draft tools create reversible artefacts; release tools do
something that cannot be recalled. A release call requires an approval record
whose content hash matches the exact content (sha256 of the canonical payload);
if content changes after approval, the approval lapses and the owner approves
again. Release calls carry an idempotency key. After an uncertain outcome the
harness never retries on its own; it marks the run for the owner to check.
Nodes may only call tools on their declared list; any other call is refused and
logged to the audit table.

## Skill library and evaluation sets

Adopted from the candidate-base research (Hermes Agent, OpenClaw — both use the
agentskills.io convention): rubrics, tone rules and templates live as
version-controlled `skills/<name>/SKILL.md` files, never inside code. Every run
records the library version (content hash) it used. The agent never edits this
library — changes arrive only through the weekly learning loop as owner-approved
diffs (PRD's rule, deliberately inverting Hermes' autonomous skill creation).

Evaluation sets live under `evaluation/sets/` as JSONL with owner scores
attached. Test suites run against recorded model responses
(`tests/recordings/`) so tests make no model calls and cost nothing; a smaller
live suite will be added for release checks.
