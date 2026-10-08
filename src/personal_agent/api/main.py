"""Web app / API — Foundation phase.

The approval queue is the heart: every outbound action waits here, the exact
content is shown before anything can be released, and an approval matches the
content by hash. Minimal server-rendered UI for now; the full interface
(Chidimma-OS-style app) arrives in a later phase and talks to this API.
"""

from __future__ import annotations

import hmac
from html import escape

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field

from personal_agent.config import Settings, load_settings
from personal_agent.db import Database
from personal_agent.engine.runner import Engine, UnknownWorkflow
from personal_agent.engine.sponsorship_triage import CLIENT as SPONSORSHIP_CLIENT
from personal_agent.harness.models import ModelGateway


class RunCreate(BaseModel):
    workflow: str = Field(min_length=1, max_length=80)
    client: str = Field(min_length=1, max_length=80)
    inputs: dict = Field(default_factory=dict)


class ApprovalDecision(BaseModel):
    decision: str = Field(pattern="^(approved|rejected|sent_back)$")
    comment: str | None = Field(default=None, max_length=4000)


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
        run_id = engine.start_run("sponsorship-triage", SPONSORSHIP_CLIENT, body.model_dump())
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
            )
        try:
            return engine.approve(run_id, body.decision, body.comment)
        except KeyError:
            raise HTTPException(404, "run not found")
        except ValueError as e:
            raise HTTPException(409, str(e))

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
            }.get(item.get("action"), "Approve — release exactly this content")
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
 button {{ padding: .4rem .8rem; border-radius: 6px; border: 1px solid #1a1a1a; background: #fff; cursor: pointer; }}
 .approve {{ background: #1a5c3f; color: #fff; }}
 .reject {{ background: #7a2c22; color: #fff; }}
</style></head>
<body>
<h1>Approval queue</h1>
<p>Nothing leaves the system without your approval. The exact content is shown before any release.</p>
{listing}
</body></html>"""

    return app


app = create_app()
