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
- **Microsoft Graph mail** — with the four `MS_GRAPH_*` settings, an approved
  sponsorship reply is sent from the one configured Outlook mailbox, in the
  original thread, to exactly the approved address with exactly the approved
  subject and body; and triage reads earlier mail with the sender. A refusal
  before the send returns the reply to the queue for a second approval; a
  dropped connection during the send stops the run and is never retried.
  Without the settings, replies are held in the local outbox as before. Tested
  against a stand-in for Microsoft's servers, not a live tenant. Setup:
  `deploy/microsoft-graph.md`.
- **Calendar** — on the pursue route the agent reads the owner's free and busy
  times through Microsoft Graph and proposes two free times for a call, picked
  in code inside the hours in `config/calendar.yaml` (provisional values). It
  reads the calendar again before sending. Without a calendar connection the
  reply asks the sender for dates. When the sender's answer picks one of the
  two times, the queue shows a calendar event to approve; approving creates it
  and Microsoft sends the invitation. An answer that picks neither is
  escalated. A later mail from that sender asking to cancel the call, or to
  move it, leads to a cancellation or a move of the same event for the owner
  to approve. The Calls screen (`/calendar`) lists the calls the agent has
  scheduled, and the owner can start a move or a cancellation there; it goes
  to the approval queue before anything changes.
- **Sign-in** — one owner, a password and a one-time code from an
  authenticator app (`src/personal_agent/auth.py`). With the three login
  secrets set, every route needs a session except `/health`, `/login` and the
  mail watcher's hook. Five failed attempts lock sign-in for fifteen minutes.
  On the server (`PERSONAL_AGENT_REQUIRE_LOGIN=1`, set in the compose file) the
  app refuses every request until the secrets exist; on a developer's machine
  with none set it serves open. Create the secrets with
  `python -m personal_agent.auth`.
- **Interface** — the five screens share one shell and stylesheet
  (`src/personal_agent/api/ui.py`), following the owner's Stitch design for
  Chidimma OS: a dark rail, a slate canvas, white cards, a serif for display.
  Four of that design's colours were darkened to meet contrast minimums; the
  file lists each with its measured ratio. The design's two typefaces (Playfair
  Display and Inter, SIL Open Font License) ship in `api/static/fonts` and load
  from this server; the pages call no outside host.
- **Going live** — `deploy/go-live.md` is the checklist for ijeoma.pro on the
  Hostinger VPS.
- **Restricted clients** — a client marked restricted on `/files` has its runs
  limited to the providers listed in `config/providers.yaml` as approved for
  confidential work. The check is in the model gateway: a model from any other
  provider is never called. If a tier's primary model is not approved and its
  named fallback is, the fallback is used and shown as such on the run; if
  neither is, the run stops at that step with the reason. The list ships
  empty, so until the owner approves a provider a restricted client's runs
  stop at their first model step. The weekly learning review leaves restricted
  clients' material out.
- **Read aloud** — each item on the queue page has a Read aloud button that
  speaks its subject, notes and content with the browser's own speech
  synthesis; pressing it again stops. Only an on-device voice is used, so the
  text is not sent to a voice provider; a browser with no on-device voice says
  so instead of speaking. For dictation, a system-wide tool such as OpenWhispr
  types into the page's text boxes with nothing to integrate.
- **Client retention and deletion** — `/files` lists clients with an upload
  box, a retention period and a delete control. A retention period (days, or
  empty to keep everything) removes that client's files by when they were last
  changed and the content of its finished runs by their last activity; run it
  daily with `python -m personal_agent.retention`. Deleting a client removes
  its folder and the content of every run made for it, everywhere that content
  is kept: the run record, the trace, tool-call results, checkpoints, owner
  edits, and for sponsorship the ledger rows and held mail. Open runs are
  cancelled. What stays: each run's row with its status, times and cost; the
  client's name on those rows; and the audit log, which is append-only and can
  hold file names, owner comments and route reasons. **Backups are not
  reached:** material deleted here remains in earlier backups until they
  expire.
- **Runs screen** — `/inspect` lists runs; `/inspect/<run_id>` shows one: each
  step in order with its status and duration, every model call in it (tier,
  model, tokens, cost, prompt version, inputs and output, including invalid
  attempts that were retried), its tool calls, what it wrote to the run, and
  cost by node. `GET /runs/<run_id>/trace` gives the same as JSON. The trace is
  kept in the `run_steps` and `model_calls` tables; long text is shortened
  there and marked as shortened. Runs made before this have totals only.
- **Settings and cost** — `/cost` shows the month's spend by workflow and by
  client (`GET /costs?month=2026-10` for the figures), the monthly cap, the
  route table, the per-run ceilings and the connected tools. It displays the
  settings; they are changed in `config/` and the server environment. The
  monthly cap (`monthly_cap_usd` in `config/budgets.yaml`, unset until the
  owner chooses one) shows a banner at 80 percent and, at 100 percent, refuses
  runs the mail watcher or a schedule starts; runs the owner starts from the
  API or the pages still go ahead. A run's cost counts in the UTC month it was
  created.
- **Weekly learning review** — groups what the owner edited, sent back and
  rejected by workflow, and proposes changes to that workflow's skill file as
  diffs. The owner accepts or declines each at `/library`; an accepted change
  is written to the skill file and committed to git, and nothing changes
  otherwise. The same job produces the weekly report (edit rate, rejection
  rate, cost and failed runs by workflow). Run it with
  `python -m personal_agent.learning` from a weekly cron, or from the button on
  `/library`. See `docs/adr/0007-learning-review.md`.
- **Strategy drafting graph** — intake (goal and source documents) → questions
  (up to five; the run waits for input) → outline → **owner approves the
  outline** → draft one section at a time → critique (figures in code, then a
  separate model call) → revise, twice at most → **owner review** → save to the
  client folder. At either pause the owner approves, edits then approves, or
  sends it back with a comment for a new pass; the difference between a draft
  and the approved version is recorded (`GET /runs/{id}/edits`). Templates and
  the writing standard are provisional. See `docs/adr/0006-strategy-drafting.md`.
- **Creative review graph** — receive files (work, brief, calibration examples)
  → render pages (PDFium; images and text, no model) → score by brief (strong,
  with the page images, in groups) → write feedback (standard, for a designer
  or a copywriter) → verify links (code; returns twice at most) → owner
  approval → file the feedback to the client folder. PDFs and image files
  render to page images; Word and text files are reviewed on text alone;
  PowerPoint must be exported to PDF. The rubric anchors are provisional and
  there are no calibration examples yet. See
  `docs/adr/0005-creative-review.md`.
- **Sponsorship triage graph** — new enquiry → classify (cheap) → look up
  history (ledger) → score and route (strong; the route is decided in code from
  `config/sponsorship.yaml`) → draft reply (standard) → check reply (facts in
  code, then a model; returns twice at most) → owner approval → send and log.
  Enquiries arrive through `POST /hooks/n8n/sponsorship` (shared secret); the
  ledger is at `GET /ledger` and the owner corrects rows with `PATCH`. **Mail is
  not connected:** an approved reply is held in a local outbox (`GET /outbox`)
  and reported as not sent. The criteria and thresholds are provisional. See
  `docs/adr/0004-sponsorship-triage.md`.
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

Draft a strategy document (`document_type` is `strategy`, `brief` or `memo`;
leave out `source_paths` to read the whole client folder). If the run asks
questions, answer at `/queue` or `POST /runs/<run_id>/input`:

```bash
curl -s localhost:8000/runs -X POST -H 'content-type: application/json' \
  -d '{"workflow":"strategy-drafting","client":"demo","inputs":{"goal":"A go-to-market strategy for the SME account","document_type":"strategy","source_paths":["research.pdf"]}}'
```

Review creative work against its brief (both already uploaded to the client
folder; `discipline` is who the feedback is for):

```bash
curl -s localhost:8000/runs -X POST -H 'content-type: application/json' \
  -d '{"workflow":"creative-review","client":"demo","inputs":{"work_paths":["poster.pdf"],"brief_path":"brief.pdf","discipline":"designer","work_kind":"outdoor"}}'
```

Post a sponsorship enquiry the way n8n will (set `PERSONAL_AGENT_N8N_SECRET`
before starting the server; without it the hook answers 503):

```bash
curl -s localhost:8000/hooks/n8n/sponsorship -X POST -H 'content-type: application/json' \
  -H "x-agent-secret: $PERSONAL_AGENT_N8N_SECRET" \
  -d '{"message_id":"m-1","sender":"ada@example.org","sender_name":"Ada","subject":"Partnership","body":"We would like your bank as a partner for 8m naira."}'
```

With no provider keys configured the model call will fail honestly — runs that
need a model stop with a clear reason. Tests never call a provider.

## Deployment (Hostinger VPS)

`docker-compose.yml` runs Postgres (internal network only), the API and Caddy
(automatic HTTPS, single entry point). Before loading any client material,
complete `deploy/hardening.md`: firewall to SSH/HTTP/HTTPS, key-only SSH,
automatic security updates, daily encrypted off-server backups with a monthly
restore drill, and one secret per provider in the server environment.

The image carries `config/` and `skills/` under `/app` and sets
`PERSONAL_AGENT_ROUTE_TABLE`, `PERSONAL_AGENT_BUDGETS`,
`PERSONAL_AGENT_SPONSORSHIP` and `PERSONAL_AGENT_SKILLS_DIR` to them, so a
change to a route, budget, threshold or skill needs `docker compose up -d
--build`. Client folders live in `./data` beside the compose file, mounted at
`/app/data`; they survive rebuilds and are what `deploy/backup.sh` copies. App
tables (runs, approvals, audit, ledger) and LangGraph checkpoints are in
Postgres: when `PERSONAL_AGENT_DATABASE_URL` is a Postgres URL the engine uses
the Postgres checkpointer and creates its tables at startup; with a SQLite URL
(local development, tests) it uses the SQLite checkpoint file.

Deploy with `deploy/deploy.sh`. It pulls the checkout's branch (fast-forward
only), builds the api image, runs the Postgres checkpointer test in a throwaway
container of that image, and only if the test passes replaces the running
containers and waits for `/health`. A failed or skipped test stops the deploy
with the old api still running.

That test (`tests/test_postgres_checkpointer.py`) is skipped everywhere but on
the server. It starts a run, leaves it waiting for approval, and approves it
from a fresh engine, all in a scratch database it creates on the compose
Postgres and drops afterwards. To run it alone:

```bash
docker compose run --rm -v "$PWD/tests:/app/tests:ro" api sh -c "uv pip install --system pytest httpx && python -m pytest tests/test_postgres_checkpointer.py -q -p no:cacheprovider"
```

## Next phases (PRD delivery order)

1. Brief evaluation: the graph, the evaluation runner and both safety sets
   exist, and the separation set passes. Still owed: the owner-scored
   evaluation set, and a live run of it and of the injection set (gate: both
   pass on the owner's real briefs).
2. Sponsorship triage: the graph, ledger and webhook exist. Still owed: the
   owner's criteria, thresholds and amount limit; the Microsoft Graph
   application (read mail and calendar, Outlook drafts, send); the n8n watcher
   itself; an owner-scored evaluation set.
3. Creative review and drafting: both graphs exist. Still owed: the owner's
   validation of the rubric, templates and writing standard; calibration
   examples; an owner-scored creative set that includes design work; live runs
   of both; and, for drafting, enough approved documents to read an edit rate.
4. Learning and tools: the weekly learning review and report exist. Still
   owed: the weekly schedule on the server and a skills directory there that
   is a git checkout; edit capture for the workflows other than drafting;
   Canva/Higgsfield via MCP. The cost and runs screens exist; an alert that
   reaches the owner outside the app does not.
