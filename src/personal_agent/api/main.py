"""Web app / API — Foundation phase.

The approval queue is the heart: every outbound action waits here, the exact
content is shown before anything can be released, and an approval matches the
content by hash. Minimal server-rendered UI for now; the full interface
(Chidimma-OS-style app) arrives in a later phase and talks to this API.
"""

from __future__ import annotations

import hmac
import json
from html import escape

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field

from personal_agent.config import Settings, load_settings
from personal_agent.db import Database
from personal_agent import costs, learning, retention, tracing
from personal_agent.engine.runner import Engine, UnknownWorkflow
from personal_agent.skills.loader import load_library
from personal_agent.engine.sponsorship_triage import CLIENT as SPONSORSHIP_CLIENT
from personal_agent.harness.models import ModelGateway


class RunCreate(BaseModel):
    workflow: str = Field(min_length=1, max_length=80)
    client: str = Field(min_length=1, max_length=80)
    inputs: dict = Field(default_factory=dict)


class ApprovalDecision(BaseModel):
    decision: str = Field(pattern="^(approved|rejected|sent_back)$")
    comment: str | None = Field(default=None, max_length=4000)
    # Edit then approve: the owner's version of the body, for items that allow it.
    edited_body: str | None = Field(default=None, max_length=400_000)


class ReviewRequest(BaseModel):
    days: int = Field(default=7, ge=1, le=90)


class ProposalDecision(BaseModel):
    decision: str = Field(pattern="^(accepted|declined)$")
    note: str | None = Field(default=None, max_length=2000)


class OwnerInput(BaseModel):
    answers: str = Field(min_length=1, max_length=20_000)


class ClientCreate(BaseModel):
    name: str = Field(min_length=1, max_length=80)


class InboundMail(BaseModel):
    """What the n8n mailbox watcher posts for each new mail."""

    message_id: str = Field(min_length=1, max_length=500)
    sender: str = Field(min_length=3, max_length=320)
    sender_name: str | None = Field(default=None, max_length=200)
    subject: str = Field(default="", max_length=1000)
    body: str = Field(min_length=1, max_length=200_000)
    received_at: str | None = Field(default=None, max_length=64)


class LedgerCorrection(BaseModel):
    received_at: str | None = None
    sender: str | None = None
    organisation: str | None = None
    request: str | None = None
    amount: str | None = None
    route: str | None = None
    decision: str | None = None
    outcome: str | None = None


NAV = (
    '<nav><a href="/queue">Approval queue</a> <a href="/inspect">Runs</a> <a href="/files">Clients</a> '
    '<a href="/library">Skill library</a> <a href="/cost">Settings and cost</a></nav>'
)

PAGE_STYLE = """
 body { font-family: Georgia, serif; max-width: 760px; margin: 2rem auto; padding: 0 1rem; background: #faf9f6; }
 h1 { border-bottom: 2px solid #1a1a1a; }
 nav a { margin-right: 1rem; font-size: .9rem; }
 .meta { color: #666; font-size: .85rem; }
 .total { font-size: 2rem; margin: .5rem 0; }
 .month a { margin: 0 .75rem; }
 table { border-collapse: collapse; width: 100%; font-size: .9rem; }
 th, td { text-align: left; padding: .3rem .5rem; border-bottom: 1px solid #ddd; }
 .bar { background: #e6e3da; border-radius: 6px; height: .6rem; margin-top: .3rem; }
 .fill { height: 100%; border-radius: 6px; background: #1a5c3f; }
 .fill.alert { background: #b7791f; }
 .fill.stopped { background: #7a2c22; }
 .banner { background: #fbf1d9; border: 1px solid #b7791f; border-radius: 6px; padding: .6rem .8rem; margin: 1rem 0; }
 .banner.stop { background: #f8e3df; border-color: #7a2c22; }
 .card { background: #fff; border: 1px solid #ddd; border-radius: 8px; padding: .6rem 1rem; margin: .75rem 0; }
 .card h3 { margin: .2rem 0; }
 .call { border-left: 3px solid #e6e3da; padding-left: .75rem; margin: .5rem 0; }
 pre { white-space: pre-wrap; background: #f4f3ef; padding: .75rem; border-radius: 6px; overflow-x: auto; font-size: .8rem; }
 .error { color: #b3261e; }
 details { margin: .3rem 0; }
 [class^="s-"] { font-size: .8rem; font-weight: normal; padding: .1rem .4rem; border-radius: 4px; background: #e6e3da; }
 .s-done { background: #dcebe3; } .s-failed, .s-cancelled { background: #f8e3df; }
 .s-waiting, .s-waiting_for_approval, .s-waiting_for_input { background: #fbf1d9; }
"""


def create_app(settings: Settings | None = None, gateway: ModelGateway | None = None) -> FastAPI:
    settings = settings or load_settings()
    db = Database(settings.database_url)
    engine = Engine(settings, db, gateway=gateway)

    app = FastAPI(title="Personal Agent", version="0.1.0 (foundation)")

    @app.get("/health")
    def health() -> dict:
        return {"ok": True}

    @app.post("/clients", status_code=201)
    def create_client(body: ClientCreate) -> dict:
        try:
            scope = engine.harness.clients.ensure(body.name)
        except Exception as e:  # invalid name
            raise HTTPException(400, str(e))
        db.audit(None, "client_created", {"client": scope.name})
        return {"client": scope.name}

    @app.get("/clients")
    def list_clients() -> list[str]:
        return engine.harness.clients.list_clients()

    @app.get("/clients/{name}/files")
    def list_files(name: str) -> list[dict]:
        try:
            scope = engine.harness.clients.scope(name)
        except KeyError:
            raise HTTPException(404, f"unknown client {name!r}")
        return engine.harness.clients.list_files(scope)

    @app.post("/clients/{name}/files", status_code=201)
    async def upload_file(name: str, request: Request) -> dict:
        form = await request.form()
        upload = form.get("file")
        if upload is None or not hasattr(upload, "read"):
            raise HTTPException(400, "multipart field 'file' is required")
        try:
            scope = engine.harness.clients.scope(name)
        except KeyError:
            raise HTTPException(404, f"unknown client {name!r}")
        data = await upload.read()
        if len(data) > 20 * 1024 * 1024:
            raise HTTPException(413, "file exceeds 20 MB intake limit")
        try:
            stored = engine.harness.clients.store_file(scope, upload.filename or "unnamed", data)
        except Exception as e:
            raise HTTPException(400, str(e))
        return {"client": scope.name, "file": stored, "bytes": len(data)}

    @app.post("/runs", status_code=201)
    def create_run(body: RunCreate) -> dict:
        try:
            run_id = engine.start_run(body.workflow, body.client, body.inputs)
        except UnknownWorkflow as e:
            raise HTTPException(400, str(e))
        return {"run_id": run_id, "run": db.get_run(run_id)}

    @app.post("/hooks/n8n/sponsorship", status_code=201)
    def sponsorship_mail(body: InboundMail, x_agent_secret: str | None = Header(default=None)) -> dict:
        # n8n holds the mailbox watcher and nothing else: this hook can only
        # start a triage run, and only with the shared secret.
        if not settings.n8n_secret:
            raise HTTPException(503, "the n8n webhook is not configured")
        if not x_agent_secret or not hmac.compare_digest(x_agent_secret, settings.n8n_secret):
            db.audit(None, "webhook_refused", {"hook": "n8n/sponsorship"})
            raise HTTPException(401, "bad or missing secret")
        seen = db.find_run_by_message("sponsorship-triage", body.message_id)
        if seen:  # the watcher polls; a repeated delivery starts nothing
            return {"run_id": seen["id"], "status": seen["status"], "duplicate": True}
        try:  # started by the watcher, not by hand: refused once the monthly cap is reached
            run_id = engine.start_run("sponsorship-triage", SPONSORSHIP_CLIENT, body.model_dump(), by_hand=False)
        except costs.MonthlyCapReached as e:
            raise HTTPException(429, str(e))
        return {"run_id": run_id, "status": db.get_run(run_id)["status"], "duplicate": False}

    @app.get("/ledger")
    def ledger() -> list[dict]:
        return db.list_ledger()

    @app.patch("/ledger/{ledger_id}")
    def correct_ledger(ledger_id: str, body: LedgerCorrection) -> dict:
        # The owner can correct any row; every correction is audited.
        before = db.get_ledger_row(ledger_id)
        if not before:
            raise HTTPException(404, "ledger row not found")
        changes = body.model_dump(exclude_unset=True)
        if changes:
            db.update_ledger_row(ledger_id, **changes)
            db.audit(before["run_id"], "ledger_corrected", {
                "ledger_id": ledger_id,
                "before": {k: before[k] for k in changes},
                "after": changes,
            })
        return db.get_ledger_row(ledger_id)

    @app.get("/outbox")
    def outbox() -> list[dict]:
        # Approved mail held locally because Microsoft Graph is not connected.
        return db.list_outbox()

    @app.get("/runs")
    def list_runs(status: str | None = None) -> list[dict]:
        return db.list_runs(status=status)

    @app.get("/runs/{run_id}")
    def get_run(run_id: str) -> dict:
        run = db.get_run(run_id)
        if not run:
            raise HTTPException(404, "run not found")
        return run

    @app.get("/approvals/pending")
    def pending() -> list[dict]:
        return engine.queue()

    @app.post("/runs/{run_id}/approval")
    async def decide(run_id: str, request: Request) -> dict:
        # Accepts JSON (API clients) and form-encoded posts (the queue page buttons).
        if request.headers.get("content-type", "").startswith("application/json"):
            body = ApprovalDecision(**(await request.json()))
        else:
            form = await request.form()
            body = ApprovalDecision(
                decision=str(form.get("decision", "")),
                comment=(str(form["comment"]) if form.get("comment") else None),
                # A browser posts a textarea with CRLF line ends; the content uses LF.
                edited_body=(str(form["edited_body"]).replace("\r\n", "\n") if "edited_body" in form else None),
            )
        try:
            return engine.approve(run_id, body.decision, body.comment, body.edited_body)
        except KeyError:
            raise HTTPException(404, "run not found")
        except ValueError as e:
            raise HTTPException(409, str(e))

    @app.get("/inputs/pending")
    def pending_inputs() -> list[dict]:
        return engine.input_requests()

    @app.post("/runs/{run_id}/input")
    async def give_input(run_id: str, request: Request) -> dict:
        # The owner's answers to a run that asked questions and is waiting.
        if request.headers.get("content-type", "").startswith("application/json"):
            body = OwnerInput(**(await request.json()))
        else:
            form = await request.form()
            body = OwnerInput(answers=str(form.get("answers", "")).replace("\r\n", "\n"))
        try:
            return engine.provide_input(run_id, body.answers)
        except KeyError:
            raise HTTPException(404, "run not found")
        except ValueError as e:
            raise HTTPException(409, str(e))

    @app.get("/runs/{run_id}/edits")
    def owner_edits(run_id: str) -> list[dict]:
        # What the owner changed before approving, kept for the learning loop.
        return db.list_owner_edits(run_id)

    # --- clients: files, retention, deletion ------------------------------------------
    def _client_view(name: str) -> dict:
        files = engine.harness.clients.list_files(engine.harness.clients.scope(name))
        return {
            "client": name,
            "files": len(files),
            "bytes": sum(f["size"] for f in files),
            "runs": len(db.list_runs_for_client(name)),
            "retention_days": db.get_retention_days(name),
        }

    async def _fields(request: Request) -> dict:
        if request.headers.get("content-type", "").startswith("application/json"):
            return await request.json()
        return dict(await request.form())

    @app.get("/clients/{name}")
    def client_detail(name: str) -> dict:
        try:
            return _client_view(name)
        except KeyError:
            raise HTTPException(404, f"unknown client {name!r}")

    @app.post("/clients/{name}/retention")
    async def set_client_retention(name: str, request: Request) -> dict:
        # days: how long this client's material is kept. Empty: until the client is deleted.
        raw = (await _fields(request)).get("days")
        try:
            days = None if raw in (None, "") else int(raw)
            return retention.set_retention(engine, name, days)
        except KeyError:
            raise HTTPException(404, f"unknown client {name!r}")
        except ValueError as e:
            raise HTTPException(400, str(e))

    @app.post("/retention/apply")
    def apply_retention() -> list[dict]:
        # Removes what is past each client's period now; the daily job does the same.
        return retention.apply_retention(engine)

    @app.post("/clients/{name}/delete")
    async def delete_client(name: str, request: Request) -> dict:
        # Irreversible, so the request must repeat the client's name.
        if str((await _fields(request)).get("confirm", "")) != name:
            raise HTTPException(400, "to delete a client, send its exact name as 'confirm'")
        try:
            return retention.delete_client(engine, name)
        except KeyError:
            raise HTTPException(404, f"unknown client {name!r}")

    @app.get("/files", response_class=HTMLResponse)
    def clients_page() -> str:
        cards = []
        for name in engine.harness.clients.list_clients():
            c = _client_view(name)
            kept = "until you delete the client" if c["retention_days"] is None else f"{c['retention_days']} days"
            cards.append(
                f"""<section class="card">
                  <h3>{escape(name)}</h3>
                  <p class="meta">{c["files"]} files · {c["bytes"] / 1024:.0f} KB · {c["runs"]} runs · kept {kept}</p>
                  <form method="post" action="/clients/{escape(name)}/files" enctype="multipart/form-data">
                    <input type="file" name="file"/> <button>Upload</button>
                  </form>
                  <form method="post" action="/clients/{escape(name)}/retention">
                    <label>Keep for <input type="number" name="days" min="1" size="5"
                      value="{c["retention_days"] or ""}"/> days</label>
                    <button>Set</button> <span class="meta">empty keeps everything</span>
                  </form>
                  <details><summary>Delete this client</summary>
                    <p>Removes the folder and the content of every run made for {escape(name)}: briefs, drafts,
                     scores, replies and model inputs and outputs. Run costs and the audit log stay. This cannot be undone.</p>
                    <form method="post" action="/clients/{escape(name)}/delete">
                      <input type="text" name="confirm" placeholder="type {escape(name)} to confirm"/>
                      <button class="reject">Delete {escape(name)}</button>
                    </form>
                  </details>
                </section>"""
            )
        return f"""<!doctype html>
<html><head><meta charset="utf-8"/><title>Clients — Personal Agent</title><style>{PAGE_STYLE}
 form {{ margin: .4rem 0; }} .reject {{ background: #7a2c22; color: #fff; }}
 button {{ padding: .3rem .7rem; border-radius: 6px; border: 1px solid #1a1a1a; background: #fff; cursor: pointer; }}
</style></head>
<body>
{NAV}
<h1>Clients</h1>
<p>Each client has its own folder; a run sees only its own client's files. A retention period removes files and
 run content older than it, once a day.</p>
<form method="post" action="/retention/apply"><button>Apply retention periods now</button></form>
{"".join(cards) or "<p>No clients yet.</p>"}
</body></html>"""

    # --- runs screen ------------------------------------------------------------------
    @app.get("/runs/{run_id}/trace")
    def run_trace(run_id: str) -> dict:
        trace = tracing.build_trace(db, run_id)
        if trace is None:
            raise HTTPException(404, "run not found")
        return trace

    @app.get("/inspect", response_class=HTMLResponse)
    def runs_page(status: str | None = None) -> str:
        runs = db.list_runs(status=status or None)[:200]
        rows = "".join(
            f'<tr><td><a href="/inspect/{r["id"]}">{escape(r["created_at"][:16].replace("T", " "))}</a></td>'
            f'<td>{escape(r["workflow"])}</td><td>{escape(r["client"])}</td>'
            f'<td class="s-{escape(r["status"])}">{escape(r["status"].replace("_", " "))}</td>'
            f'<td>${(r["cost_usd"] or 0):.2f}</td></tr>'
            for r in runs
        )
        filters = " ".join(
            f'<a href="/inspect{"?status=" + s if s else ""}">{label}</a>'
            for s, label in [("", "all"), ("waiting_for_approval", "waiting for approval"),
                             ("waiting_for_input", "waiting for input"), ("failed", "failed"), ("done", "done")]
        )
        table = (
            f"<table><tr><th>Started (UTC)</th><th>Workflow</th><th>Client</th><th>Status</th><th>Cost</th></tr>{rows}</table>"
            if runs else "<p>No runs.</p>"
        )
        return f"""<!doctype html>
<html><head><meta charset="utf-8"/><title>Runs — Personal Agent</title><style>{PAGE_STYLE}</style></head>
<body>
{NAV}
<h1>Runs</h1>
<p class="meta">Show: {filters}. The most recent 200.</p>
{table}
</body></html>"""

    @app.get("/inspect/{run_id}", response_class=HTMLResponse)
    def run_page(run_id: str) -> str:
        trace = tracing.build_trace(db, run_id)
        if trace is None:
            raise HTTPException(404, "run not found")
        run = trace["run"]

        def block(title: str, value) -> str:
            if value in (None, [], {}, ""):
                return ""
            text = value if isinstance(value, str) else json.dumps(value, indent=2, ensure_ascii=False)
            return f"<details><summary>{title}</summary><pre>{escape(text)}</pre></details>"

        cards = []
        for step in trace["steps"]:
            calls = "".join(
                f"""<div class="call">
                  <p class="meta">model call, attempt {c["attempt"]} · {escape(c["tier"] or "")} tier ·
                   {escape(c["model"] or "no model reached")}{" (fallback)" if c["fallback_used"] else ""} ·
                   {(c["tokens_in"] or 0):,} in, {(c["tokens_out"] or 0):,} out · ${(c["cost_usd"] or 0):.4f} ·
                   prompt {escape(c["prompt_version"] or "")} ·
                   <span class="s-{'done' if c['outcome'] == 'ok' else 'failed'}">{escape(c["outcome"])}</span></p>
                  {f'<p class="error">{escape(c["error"])}</p>' if c["error"] else ""}
                  {block("Inputs", c["input"])}{block("Output", c["output"])}
                </div>"""
                for c in step["model_calls"]
            )
            tools = "".join(
                f'<p class="meta">tool call · {escape(t["tool"])} ({escape(t["tool_class"])}) · '
                f'<span class="s-{"done" if t["outcome"] in ("executed", "replay") else "failed"}">'
                f'{escape(t["outcome"].replace("_", " "))}</span></p>{block("Result", t["detail"])}'
                for t in step["tool_calls"]
            )
            duration = "" if step["duration_ms"] is None else f' · {step["duration_ms"] / 1000:.1f}s'
            kind = "" if step["model_calls"] else " · code, no model"
            cards.append(
                f"""<section class="card">
                  <h3>{step["seq"]}. {escape(step["node"])}
                   <span class="s-{escape(step["status"])}">{escape(step["status"])}</span></h3>
                  <p class="meta">${step["cost_usd"]:.4f} · {step["tokens"]:,} tokens{duration}{kind}</p>
                  {f'<p class="error">{escape(step["error"])}</p>' if step["error"] else ""}
                  {calls}{tools}{block("What the step wrote to the run", step["output"])}
                </section>"""
            )
        by_node = "".join(
            f"<tr><td>{escape(n['node'])}</td><td>{n['steps']}</td><td>{n['model_calls']}</td>"
            f"<td>{n['tokens']:,}</td><td>${n['cost_usd']:.4f}</td></tr>"
            for n in trace["cost_by_node"]
        )
        untraced = (
            f'<p class="meta">${trace["untraced_cost_usd"]:.4f} of this run\'s cost has no per-node record '
            "(it ran before tracing was added, or a call's cost could not be read).</p>"
            if trace["untraced_cost_usd"] > 0.00005 else ""
        )
        prompts = ", ".join(f"{escape(node)} {escape(v)}" for node, v in trace["prompt_versions"]) or "none"
        flags = "".join(f"<li>{escape(str(f))}</li>" for f in run["flags"] or [])
        return f"""<!doctype html>
<html><head><meta charset="utf-8"/><title>Run {escape(run["id"][:8])} — Personal Agent</title>
<style>{PAGE_STYLE}</style></head>
<body>
{NAV}
<h1>{escape(run["workflow"])} <span class="s-{escape(run["status"])}">{escape(run["status"].replace("_", " "))}</span></h1>
<p class="meta">run {escape(run["id"])} · client {escape(run["client"])} · started {escape(run["created_at"])} ·
 updated {escape(run["updated_at"])}</p>
{f'<p class="error">{escape(run["error_reason"])}</p>' if run["error_reason"] else ""}
<p class="total">${(run["cost_usd"] or 0):.4f} <span class="meta">{(run["tokens_in"] or 0):,} tokens in,
 {(run["tokens_out"] or 0):,} out{" · a fallback model was used" if run["fallback_used"] else ""}</span></p>
<p class="meta">skill library {escape((run["skill_library_version"] or "")[:12])} · prompts: {prompts}</p>
{f'<ul class="meta">{flags}</ul>' if flags else ""}
{block("Run inputs", tracing.clip(run["inputs"]))}{block("Run outputs", tracing.clip(run["outputs"]))}
<h2>Cost by node</h2>
{f'<table><tr><th>Node</th><th>Steps</th><th>Model calls</th><th>Tokens</th><th>Cost</th></tr>{by_node}</table>' if by_node else '<p>No steps were recorded for this run.</p>'}
{untraced}
<h2>Steps</h2>
{"".join(cards) or "<p>No steps were recorded for this run.</p>"}
{block("Tool calls not tied to a step", trace["unplaced_tool_calls"])}
</body></html>"""

    # --- cost and settings ------------------------------------------------------------
    def _cap_banner() -> str:
        cap = costs.current_cap(db, settings)
        if cap["state"] == "alert":
            return (f'<div class="banner">Spend this month is at {cap["used"]:.0%} of the '
                    f'${cap["cap_usd"]:.2f} cap.</div>')
        if cap["state"] == "stopped":
            return (f'<div class="banner stop">The ${cap["cap_usd"]:.2f} monthly cap is reached. Runs started '
                    "by the mail watcher or a schedule are refused; runs you start still go ahead.</div>")
        return ""

    @app.get("/costs")
    def cost_summary(month: str | None = None) -> dict:
        try:
            return costs.summary(db, settings, month)
        except ValueError as e:
            raise HTTPException(400, str(e))

    @app.get("/cost", response_class=HTMLResponse)
    def cost_page(month: str | None = None) -> str:
        try:
            s = costs.summary(db, settings, month)
        except ValueError as e:
            raise HTTPException(400, str(e))
        before, after = costs.adjacent_months(s["month"])

        def table(title: str, rows: list[dict]) -> str:
            if not rows:
                return f"<h2>{title}</h2><p>No runs this month.</p>"
            body = "".join(
                f"<tr><td>{escape(r['name'])}</td><td>{r['runs']}</td><td>{r['failed']}</td>"
                f"<td>{r['tokens']:,}</td><td>${r['cost_usd']:.2f}</td></tr>"
                for r in rows
            )
            return (f"<h2>{title}</h2><table><tr><th></th><th>Runs</th><th>Failed</th><th>Tokens</th>"
                    f"<th>Cost</th></tr>{body}</table>")

        cap = s["cap"]
        if cap["state"] == "none":
            cap_line = "No monthly cap is set (<code>monthly_cap_usd</code> in <code>config/budgets.yaml</code>)."
        else:
            width = min(100, round(cap["used"] * 100))
            cap_line = (
                f'{cap["used"]:.0%} of the ${cap["cap_usd"]:.2f} monthly cap'
                f'<div class="bar"><div class="fill {cap["state"]}" style="width:{width}%"></div></div>'
            )
        b = settings.budgets
        routes = "".join(
            f"<tr><td>{escape(tier)}</td><td>{escape(route.primary)}</td>"
            f"<td>{escape(route.fallback or 'none')}</td></tr>"
            for tier, route in settings.route_table.items()
        )
        tools = "".join(
            f"<tr><td>{escape(name)}</td><td>{escape(cls)}</td>"
            f"<td>{'owner approval for every call' if cls == 'release' else 'none'}</td></tr>"
            for name, cls in engine.harness.tools.registered()
        )
        policy = settings.sponsorship
        sponsorship = (
            "not configured: every enquiry escalates" if policy is None else
            f"pursue at {policy.pursue_at}, decline below {policy.decline_below}, escalate above "
            + ("any amount" if policy.escalate_above is None else f"{policy.escalate_above:,.0f} naira")
        )
        return f"""<!doctype html>
<html><head><meta charset="utf-8"/><title>Settings and cost — Personal Agent</title>
<style>{PAGE_STYLE}</style></head>
<body>
{NAV}
<h1>Settings and cost</h1>
{_cap_banner() if s["month"] == costs.month_bounds()[0] else ""}
<p class="month"><a href="/cost?month={before}">&larr; {before}</a> <strong>{s["month"]}</strong>
 <a href="/cost?month={after}">{after} &rarr;</a></p>
<p class="total">${s["total_usd"]:.2f} <span class="meta">across {s["runs"]} runs</span></p>
<p>{cap_line}</p>
{table("By workflow", s["by_workflow"])}
{table("By client", s["by_client"])}
<h2>Model routes</h2>
<table><tr><th>Tier</th><th>Primary</th><th>Fallback</th></tr>{routes}</table>
<h2>Ceilings for each run</h2>
<table>
<tr><td>Cost</td><td>${b.max_cost_usd:.2f}</td></tr>
<tr><td>Tokens</td><td>{b.max_total_tokens:,}</td></tr>
<tr><td>Tool calls</td><td>{b.max_tool_calls}</td></tr>
<tr><td>Elapsed time</td><td>{b.max_elapsed_seconds} seconds</td></tr>
</table>
<h2>Connected tools</h2>
<table><tr><th>Tool</th><th>Class</th><th>Approval</th></tr>{tools}</table>
<p class="meta">Mail is not connected: <code>send_mail</code> holds approved replies in the local outbox.</p>
<h2>Other settings</h2>
<table>
<tr><td>Sponsorship policy</td><td>{sponsorship}</td></tr>
<tr><td>Mail watcher webhook</td><td>{"on" if settings.n8n_secret else "off (no shared secret set)"}</td></tr>
<tr><td>Creative review</td><td>images up to {settings.creative_review.image_max_edge} px,
 {settings.creative_review.pages_per_group} pages a call, {settings.creative_review.max_pages} pages at most</td></tr>
<tr><td>Retention per client</td><td>set for each client on the <a href="/files">Clients</a> page</td></tr>
</table>
<p class="meta">These are read from the files in <code>config/</code> and the server environment.
 Change them there; this screen does not edit them.</p>
</body></html>"""

    # --- skill library and the learning loop ---------------------------------------
    def _proposal_view(proposal: dict) -> dict:
        """A proposal with its diff against the skill file as it stands now."""
        path = settings.skills_dir / proposal["skill"] / "SKILL.md"
        try:
            diff = learning.skill_diff(path.read_text(encoding="utf-8"), proposal["find"], proposal["replace"])
        except (OSError, ValueError) as e:
            diff = f"(no longer applies: {e})"
        return {**proposal, "diff": diff}

    @app.get("/skills")
    def skills() -> dict:
        version, library = load_library(settings.skills_dir)
        return {"version": version, "skills": library}

    @app.post("/learning/reviews", status_code=201)
    def start_review(body: ReviewRequest | None = None) -> dict:
        # On the owner's request; the weekly run uses `python -m personal_agent.learning`.
        return learning.run_review(engine, days=(body.days if body else 7), by_hand=True)

    @app.get("/learning/proposals")
    def proposals(status: str | None = "pending") -> list[dict]:
        return [_proposal_view(p) for p in db.list_proposals(status=status or None)]

    @app.post("/learning/proposals/{proposal_id}")
    async def decide_proposal(proposal_id: str, request: Request) -> dict:
        if request.headers.get("content-type", "").startswith("application/json"):
            body = ProposalDecision(**(await request.json()))
        else:
            form = await request.form()
            body = ProposalDecision(
                decision=str(form.get("decision", "")), note=(str(form["note"]) if form.get("note") else None)
            )
        try:
            return learning.decide(engine, proposal_id, body.decision, body.note)
        except KeyError:
            raise HTTPException(404, "proposal not found")
        except ValueError as e:
            raise HTTPException(409, str(e))

    @app.get("/library", response_class=HTMLResponse)
    def library_page() -> str:
        version, library = load_library(settings.skills_dir)
        cards = []
        for p in map(_proposal_view, db.list_proposals(status="pending")):
            cards.append(
                f"""
                <section class="card">
                  <h2>{escape(p["skill"])}</h2>
                  <p>{escape(p["rationale"] or "")}</p>
                  <p class="meta">proposal {p["id"]} · rests on runs {escape(", ".join(p["evidence"] or []))}</p>
                  <pre>{escape(p["diff"])}</pre>
                  <div class="actions">
                    <form method="post" action="/learning/proposals/{p["id"]}">
                      <input type="hidden" name="decision" value="accepted"/>
                      <button class="approve">Accept — change the skill file</button>
                    </form>
                    <form method="post" action="/learning/proposals/{p["id"]}">
                      <input type="hidden" name="decision" value="declined"/>
                      <input type="text" name="note" placeholder="why not (optional)"/>
                      <button class="reject">Decline</button>
                    </form>
                  </div>
                </section>"""
            )
        pending = "".join(cards) or "<p>No proposed changes are waiting.</p>"
        latest = next((r for r in db.list_runs() if r["workflow"] == learning.WORKFLOW and r["outputs"]), None)
        report = ""
        if latest:
            rows = "".join(
                f"<tr><td>{escape(w)}</td><td>{r['runs']}</td><td>{r['failed_runs']}</td>"
                f"<td>{'n/a' if r['edit_rate'] is None else format(r['edit_rate'], '.0%')}</td>"
                f"<td>{'n/a' if r['rejection_rate'] is None else format(r['rejection_rate'], '.0%')}</td>"
                f"<td>${r['cost_usd']:.2f}</td></tr>"
                for w, r in sorted(latest["outputs"]["report"].items())
            )
            period = latest["outputs"]["period"]
            report = f"""<h2>Weekly report</h2>
              <p class="meta">{escape(period["since"])} to {escape(period["until"])}</p>
              <table><tr><th>Workflow</th><th>Runs</th><th>Failed</th><th>Edit rate</th>
              <th>Rejection rate</th><th>Cost</th></tr>{rows}</table>"""
        files = "".join(
            f"<details><summary>{escape(name)}</summary><pre>{escape(text)}</pre></details>"
            for name, text in sorted(library.items())
        )
        return f"""<!doctype html>
<html><head><meta charset="utf-8"/><title>Skill library — Personal Agent</title>
<style>
 body {{ font-family: Georgia, serif; max-width: 760px; margin: 2rem auto; padding: 0 1rem; background: #faf9f6; }}
 h1 {{ border-bottom: 2px solid #1a1a1a; }}
 .card {{ background: #fff; border: 1px solid #ddd; border-radius: 8px; padding: 1rem 1.25rem; margin: 1rem 0; }}
 pre {{ white-space: pre-wrap; background: #f4f3ef; padding: .75rem; border-radius: 6px; overflow-x: auto; }}
 .meta {{ color: #666; font-size: .85rem; }}
 .actions {{ display: flex; gap: .5rem; flex-wrap: wrap; align-items: center; }}
 form {{ display: flex; gap: .4rem; align-items: center; }}
 button {{ padding: .4rem .8rem; border-radius: 6px; border: 1px solid #1a1a1a; background: #fff; cursor: pointer; }}
 .approve {{ background: #1a5c3f; color: #fff; }}
 .reject {{ background: #7a2c22; color: #fff; }}
 table {{ border-collapse: collapse; width: 100%; font-size: .9rem; }}
 th, td {{ text-align: left; padding: .3rem .5rem; border-bottom: 1px solid #ddd; }}
 details {{ margin: .5rem 0; }}
</style></head>
<body>
{NAV}
<h1>Skill library</h1>
<p>The agent never edits these files. A change arrives here as a proposal from the learning review and takes effect only when you accept it.</p>
<p class="meta">library version {version[:12]}</p>
<form method="post" action="/learning/reviews"><button>Run a learning review now</button></form>
<h2>Proposed changes</h2>
{pending}
{report}
<h2>Skills</h2>
{files}
</body></html>"""

    @app.get("/queue", response_class=HTMLResponse)
    def queue_page() -> str:
        rows = []
        for item in engine.queue():
            error = (
                f'<div class="error">{escape(item["error"])}</div>' if item.get("error") else ""
            )
            # Drafted content derives from untrusted material: always escaped.
            content = item["content"]
            notes = "".join(f"<li>{escape(str(note))}</li>" for note in item.get("notes", []))
            notes = f'<ul class="notes">{notes}</ul>' if notes else ""
            to = f'<p class="meta">To: {escape(content["to"])}</p>' if content.get("to") else ""
            approve = {
                "send": "Approve — send exactly this reply",
                "acknowledge": "Acknowledge — nothing is sent",
                "outline": "Approve this outline — start drafting",
                "save": "Approve — save exactly this document",
            }.get(item.get("action"), "Approve — release exactly this content")
            edit = (
                f"""<details><summary>Edit, then approve</summary>
                    <form method="post" action="/runs/{item["run_id"]}/approval" class="edit">
                      <input type="hidden" name="decision" value="approved"/>
                      <textarea name="edited_body" rows="16">{escape(content.get("body", ""))}</textarea>
                      <button class="approve">Approve my edited version</button>
                    </form></details>"""
                if item.get("editable") else ""
            )
            rows.append(
                f"""
                <section class="card">
                  <h2>{escape(content.get("subject", ""))}</h2>
                  <p class="meta">run {item["run_id"]} · {escape(item["workflow"])} · client {escape(item["client"])}</p>
                  {error}
                  {notes}
                  {to}
                  <pre>{escape(content.get("body", ""))}</pre>
                  <p class="meta">content hash: <code>{item["content_hash"]}</code></p>
                  {edit}
                  <div class="actions">
                    <form method="post" action="/runs/{item["run_id"]}/approval">
                      <input type="hidden" name="decision" value="approved"/>
                      <button class="approve">{approve}</button>
                    </form>
                    <form method="post" action="/runs/{item["run_id"]}/approval">
                      <input type="hidden" name="decision" value="sent_back"/>
                      <input type="text" name="comment" placeholder="what to change"/>
                      <button class="sendback">Send back with a comment</button>
                    </form>
                    <form method="post" action="/runs/{item["run_id"]}/approval">
                      <input type="hidden" name="decision" value="rejected"/>
                      <input type="text" name="comment" placeholder="reason (required in the real flow)"/>
                      <button class="reject">Reject</button>
                    </form>
                  </div>
                </section>"""
            )
        for ask in engine.input_requests():
            questions = "".join(f"<li>{escape(str(q))}</li>" for q in ask["questions"])
            rows.append(
                f"""
                <section class="card">
                  <h2>Questions before drafting</h2>
                  <p class="meta">run {ask["run_id"]} · {escape(ask["workflow"])} · client {escape(ask["client"])}</p>
                  <p>{escape(ask["goal"])}</p>
                  <ol>{questions}</ol>
                  <form method="post" action="/runs/{ask["run_id"]}/input" class="edit">
                    <textarea name="answers" rows="6" placeholder="Your answers"></textarea>
                    <button class="approve">Send answers — continue to the outline</button>
                  </form>
                </section>"""
            )
        listing = "".join(rows) or "<p>Nothing is waiting for your approval.</p>"
        return f"""<!doctype html>
<html><head><meta charset="utf-8"/><title>Approval queue — Personal Agent</title>
<style>
 body {{ font-family: Georgia, serif; max-width: 760px; margin: 2rem auto; padding: 0 1rem; background: #faf9f6; }}
 h1 {{ border-bottom: 2px solid #1a1a1a; }}
 .card {{ background: #fff; border: 1px solid #ddd; border-radius: 8px; padding: 1rem 1.25rem; margin: 1rem 0; }}
 pre {{ white-space: pre-wrap; background: #f4f3ef; padding: .75rem; border-radius: 6px; }}
 .meta {{ color: #666; font-size: .85rem; }}
 .notes {{ font-size: .9rem; margin: .5rem 0; padding-left: 1.1rem; }}
 .error {{ color: #b3261e; font-weight: bold; margin: .5rem 0; }}
 .actions {{ display: flex; gap: .5rem; flex-wrap: wrap; align-items: center; }}
 form {{ display: flex; gap: .4rem; align-items: center; }}
 form.edit {{ flex-direction: column; align-items: stretch; margin: .5rem 0; }}
 textarea {{ width: 100%; box-sizing: border-box; font: inherit; padding: .5rem; }}
 details {{ margin: .5rem 0; }}
 button {{ padding: .4rem .8rem; border-radius: 6px; border: 1px solid #1a1a1a; background: #fff; cursor: pointer; }}
 .approve {{ background: #1a5c3f; color: #fff; }}
 .reject {{ background: #7a2c22; color: #fff; }}
 nav a {{ margin-right: 1rem; font-size: .9rem; }}
 .banner {{ background: #fbf1d9; border: 1px solid #b7791f; border-radius: 6px; padding: .6rem .8rem; margin: 1rem 0; }}
 .banner.stop {{ background: #f8e3df; border-color: #7a2c22; }}
</style></head>
<body>
{NAV}
<h1>Approval queue</h1>
{_cap_banner()}
<p>Nothing leaves the system without your approval. The exact content is shown before any release.</p>
{listing}
</body></html>"""

    return app


app = create_app()
