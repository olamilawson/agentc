# ADR 0002: OpenMuse one-day test — verdict

Date: 2026-10-05
Decided by: Builder (owner informed)
Status: Accepted

The PRD requires a one-day test of OpenMuse (CopilotKit/openmuse) against three
requirements: it acts as an MCP client, it routes between model providers, and it
accepts a Microsoft Graph mail adapter. If it fails any, the builder uses the
default stack.

Method: shallow clone of `main` (commit 4e73a61d4f943945abfbebbd1ddf0fcd0c5ddd99
per release archive; repo created 2026-09-15, alpha) and direct source
inspection. A live run with provider keys was not performed; the three criteria
are architecture questions answerable from source.

## Findings

1. MCP client — FAIL. MCP usage exists in exactly one file,
   `apps/server/src/search.ts`: a hard-wired Parallel search MCP endpoint called
   through `@modelcontextprotocol/sdk`. There is no general MCP client registry or
   configuration by which other MCP servers (e.g. Canva) could attach without
   custom code.

2. Routes between model providers — PARTIAL/FAIL. `AGENT_BACKEND` selects
   sample/model/agui and the model backend can use OpenAI, Anthropic or Google
   keys. However `apps/server/src/config.ts:150` makes `CPK_INTELLIGENCE_API_KEY`
   a required setting in every mode ("OpenMuse requires CPK_INTELLIGENCE_API_KEY"),
   so chat persistence and the runtime depend on a CopilotKit cloud service. The
   PRD requires a model gateway "so no provider is hard-wired"; a mandatory
   third-party cloud key is a hard-wiring of the chat layer.

3. Accepts a Microsoft Graph mail adapter — FAIL.
   `packages/integrations/src/google.ts` is a concrete 816-line `GoogleClient`
   speaking to the Gmail and Calendar REST APIs directly. There is no
   MailAdapter/CalendarAdapter port; accepting a Graph adapter would first
   require introducing an interface and refactoring its call sites across
   `apps/server`.

## Decision

OpenMuse fails the test (worst case 1 partial of 3). Per the PRD, the builder
uses the default stack. See ADR 0001 for the stack and language decision.

What OpenMuse remains useful for: the "stored action review" flow (review before
any Google send) as a pattern reference for our approval gate, and its SQL-lease
task worker as a queue pattern. The shallow clone is retained at `_eval_openmuse/`
as evidence and reference.
