"""Web app and API.

The approval queue is the heart: every outbound action waits here, the exact
content is shown before anything can be released, and an approval matches the
content by hash. Minimal server-rendered UI for now; the full interface
(Chidimma-OS-style app) arrives in a later phase and talks to this API.
"""

from __future__ import annotations

import hmac
import json
from datetime import datetime, timedelta, timezone
from html import escape
from pathlib import Path
from urllib.parse import quote, urlsplit

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from personal_agent.api import ui
from personal_agent.api.ui import plural
from personal_agent.config import Settings, load_settings
from personal_agent.db import Database
from personal_agent import auth, costs, learning, retention, tracing
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


# Read aloud on the queue page, with the browser's own speech synthesis. Only an
# on-device voice is used: some browsers also offer network voices, which would
# send the text of a draft to the voice's provider.
READ_ALOUD_SCRIPT = """<script>
(function () {
  if (!('speechSynthesis' in window)) return;
  var synth = window.speechSynthesis, current = null;
  synth.getVoices();  // starts loading the voice list
  function reset() { if (current) { current.textContent = 'Read aloud'; current = null; } }
  function stop() { synth.cancel(); reset(); }
  function localVoice() {
    var local = synth.getVoices().filter(function (v) { return v.localService; });
    var lang = (navigator.language || 'en').slice(0, 2);
    return local.filter(function (v) { return v.lang.slice(0, 2) === lang; })[0] || local[0] || null;
  }
  function read(button) {
    var voice = localVoice();
    if (!voice) { button.textContent = 'No on-device voice in this browser'; return; }
    var card = button.closest('.card');
    var text = Array.prototype.map.call(card.querySelectorAll('.say'), function (e) { return e.innerText; }).join('\\n');
    // Markdown marks are for the eye; a list number or a heading sign is not read out.
    var parts = text.split(/\\n+/).map(function (line) {
      return line.replace(/^[\\s#>*-]+/, '').replace(/[*_`]/g, '').trim();
    }).filter(Boolean);
    if (!parts.length) return;
    current = button;
    button.textContent = 'Stop reading';
    // One utterance per line: some browsers cut a long utterance short.
    parts.forEach(function (part, i) {
      var u = new SpeechSynthesisUtterance(part);
      u.voice = voice;
      u.lang = voice.lang;
      if (i === parts.length - 1) u.onend = function () { if (current === button) reset(); };
      u.onerror = function () { if (current === button) reset(); };
      synth.speak(u);
    });
  }
  document.querySelectorAll('button.read').forEach(function (button) {
    button.hidden = false;
    button.addEventListener('click', function () {
      var mine = current === button;
      stop();
      if (!mine) read(button);
    });
  });
  window.addEventListener('pagehide', stop);
})();
</script>"""


def create_app(settings: Settings | None = None, gateway: ModelGateway | None = None, mail=None) -> FastAPI:
    settings = settings or load_settings()
    db = Database(settings.database_url)
    engine = Engine(settings, db, gateway=gateway, mail=mail)

    app = FastAPI(title="Personal Agent", version="0.1.0 (foundation)")

    # --- sign-in ----------------------------------------------------------------------
    # With the three login secrets set, every route needs a session except the
    # health check, the sign-in page and the mail watcher's hook (which has its
    # own secret). With none set, a server refuses every request; a developer's
    # machine serves open.
    guard = (
        auth.LoginGuard(settings.owner_password_hash, settings.totp_secret, settings.session_secret, settings.session_hours)
        if settings.owner_password_hash and settings.totp_secret and settings.session_secret
        else None
    )

    @app.middleware("http")
    async def require_session(request: Request, call_next):
        path = request.url.path
        # The fonts are public files, and the sign-in page needs them before any session exists.
        if path in ("/health", "/login") or path.startswith(("/hooks/", "/static/fonts/")):
            return await call_next(request)
        if guard is None:
            if settings.require_login:
                return PlainTextResponse(
                    "Sign-in is not set up on this server. Run `python -m personal_agent.auth` there "
                    "and add its three lines to .env.", status_code=503,
                )
            return await call_next(request)
        if not guard.valid_session(request.cookies.get(auth.SESSION_COOKIE)):
            if request.method == "GET" and "text/html" in request.headers.get("accept", ""):
                return RedirectResponse(f"/login?next={quote(path)}", status_code=303)
            return JSONResponse({"detail": "sign in first"}, status_code=401)
        # A signed-in browser must send changes from this site, not from another page.
        origin = request.headers.get("origin")
        if request.method not in ("GET", "HEAD", "OPTIONS") and origin:
            if urlsplit(origin).netloc != request.headers.get("host"):
                return PlainTextResponse("This request came from another site, so the agent refused it.", status_code=403)
        return await call_next(request)

    @app.get("/login", response_class=HTMLResponse)
    def sign_in(next: str = "/queue"):
        if guard is None:
            return RedirectResponse("/queue", status_code=303)
        return ui.sign_in_page(settings.brand_name, next_path=next)

    @app.post("/login")
    async def sign_in_submit(request: Request):
        if guard is None:
            return RedirectResponse("/queue", status_code=303)
        form = await request.form()
        target = str(form.get("next") or "/queue")
        if not target.startswith("/") or target.startswith("//"):
            target = "/queue"  # never redirect off this site
        if guard.locked():
            return HTMLResponse(ui.sign_in_page(
                settings.brand_name, "Five failed attempts locked sign-in. Try again in 15 minutes.", target
            ), status_code=429)
        if not guard.check(str(form.get("password", "")), str(form.get("code", ""))):
            db.audit(None, "login_failed", {"locked": guard.locked()})
            return HTMLResponse(ui.sign_in_page(
                settings.brand_name, "The password or the code did not match. Try again.", target
            ), status_code=401)
        db.audit(None, "login_succeeded", None)
        response = RedirectResponse(target, status_code=303)
        response.set_cookie(
            auth.SESSION_COOKIE, guard.new_session(), max_age=settings.session_hours * 3600,
            httponly=True, samesite="lax", secure=settings.require_login, path="/",
        )
        return response

    @app.post("/logout")
    def sign_out():
        response = RedirectResponse("/login", status_code=303)
        response.delete_cookie(auth.SESSION_COOKIE, path="/")
        return response

    def render(title: str, active: str, body: str, script: str = "") -> str:
        waiting = len(db.list_runs(status="waiting_for_approval")) + len(db.list_runs(status="waiting_for_input"))
        return ui.page(
            title, active, body, brand=settings.brand_name, waiting=waiting,
            signed_in=guard is not None, script=script,
        )

    app.mount("/static", StaticFiles(directory=Path(__file__).parent / "static"), name="static")

    @app.get("/")
    def home():
        return RedirectResponse("/queue", status_code=303)

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

    @app.get("/calls")
    def calls() -> list[dict]:
        # Calls the agent has put in the calendar, with their state.
        return db.list_calls()

    async def _change_call(call_id: str, request: Request, action: str):
        # The owner asks to move or cancel a call. Nothing changes here: the request
        # becomes an item in the approval queue that shows the exact change.
        from_form = not request.headers.get("content-type", "").startswith("application/json")
        fields = await _fields(request)
        if not db.get_call(call_id):
            raise HTTPException(404, "call not found")
        inputs = {"action": action, "call_id": call_id}
        if action == "move":
            inputs["start"] = str(fields.get("start") or "")
        elif str(fields.get("message") or "").strip():
            inputs["message"] = str(fields["message"]).strip()[:500]
        run = db.get_run(engine.start_run("call-change", SPONSORSHIP_CLIENT, inputs))
        if from_form:  # from the Calls screen: on to the queue, or back with the reason
            if run["status"] == "failed":
                return RedirectResponse(f"/calendar?notice={quote(run['error_reason'] or '')}", status_code=303)
            return RedirectResponse("/queue", status_code=303)
        if run["status"] == "failed":
            raise HTTPException(409, run["error_reason"])
        return {"run_id": run["id"], "status": run["status"]}

    @app.post("/calls/{call_id}/move")
    async def move_call(call_id: str, request: Request):
        return await _change_call(call_id, request, "move")

    @app.post("/calls/{call_id}/cancel")
    async def cancel_call(call_id: str, request: Request):
        return await _change_call(call_id, request, "cancel")

    @app.get("/calendar", response_class=HTMLResponse)
    def calls_page(notice: str = "") -> str:
        moment = datetime.now(timezone.utc).isoformat(timespec="minutes")
        every = db.list_calls()
        ahead = sorted((c for c in every if c["status"] == "scheduled" and c["start"] > moment), key=lambda c: c["start"])
        rest = [c for c in every if c not in ahead]
        connected = engine.harness.mail is not None
        cards = []
        for c in ahead:
            forms = f"""
                  <form method="post" action="/calls/{c["id"]}/move">
                    <label>New date and time ({escape(settings.calendar.timezone)})
                      <input type="datetime-local" name="start" required/></label>
                    <button>Review this move</button>
                  </form>
                  <form method="post" action="/calls/{c["id"]}/cancel">
                    <label>Message to the attendee
                      <input type="text" name="message" maxlength="500" placeholder="This call is cancelled."/></label>
                    <button class="reject">Review this cancellation</button>
                  </form>""" if connected else ""
            cards.append(
                f"""<section class="card">
                  <h3>{escape(c["subject"] or "Call")}</h3>
                  <p>{escape(c["label"])}</p>
                  <p class="meta">with {escape(c["sender"])}</p>{forms}
                </section>"""
            )
        earlier = "".join(
            f"<tr><td>{escape(c['label'])}</td><td>{escape(c['sender'])}</td>"
            f"<td><span class=\"s-{'cancelled' if c['status'] == 'cancelled' else 'done'}\">"
            f"{'cancelled' if c['status'] == 'cancelled' else 'past'}</span></td></tr>"
            for c in rest
        )
        return render("Calls", "calls", f"""
<h1>Calls</h1>
<p class="lede">The calls the agent has put in your calendar. A move or a cancellation goes to your approval
 queue first, and the calendar changes once you approve it there.</p>
{f'<div class="banner" role="alert">{escape(notice)}</div>' if notice else ""}
{"" if connected else '<p class="banner">No calendar connection yet, so the agent can change no call.</p>'}
{"".join(cards) or "<p>No call is scheduled. One appears here once a sender picks a time and you approve the invitation.</p>"}
{f'<h2>Earlier and cancelled</h2><div class="tablewrap"><table><tr><th>Time</th><th>With</th><th>State</th></tr>{earlier}</table></div>' if earlier else ""}
""")

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
            "restricted": db.is_restricted(name),
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

    @app.post("/clients/{name}/restricted")
    async def set_client_restricted(name: str, request: Request) -> dict:
        # restricted: this client's runs may only reach providers approved for confidential work.
        raw = (await _fields(request)).get("restricted")
        if isinstance(raw, str):
            raw = raw.strip().lower()
        if raw not in (True, False, "true", "false", "on", "off", "1", "0"):
            raise HTTPException(400, "send restricted as true or false")
        restricted = raw in (True, "true", "on", "1")
        try:
            scope = engine.harness.clients.scope(name)
        except KeyError:
            raise HTTPException(404, f"unknown client {name!r}")
        db.set_restricted(scope.name, restricted)
        db.audit(None, "client_restricted_set", {"client": scope.name, "restricted": restricted})  # a configuration change
        return {
            "client": scope.name, "restricted": restricted,
            "approved_providers": settings.confidential_providers,
        }

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
        providers = escape(", ".join(settings.confidential_providers))
        restricted_line = (
            f'<span class="s-waiting">Restricted</span> Its runs go to {providers} and no other provider.'
            if providers else
            '<span class="s-waiting">Restricted</span> You have approved no provider for confidential work, '
            "so its runs stop at the first model step."
        )
        for name in engine.harness.clients.list_clients():
            c = _client_view(name)
            kept = "until you delete the client" if c["retention_days"] is None else f"for {plural(c['retention_days'], 'day')}"
            cards.append(
                f"""<section class="card">
                  <h3>{escape(name)}</h3>
                  <p class="meta">{plural(c["files"], "file")} · {c["bytes"] / 1024:.0f} KB · {plural(c["runs"], "run")} · kept {kept}</p>
                  <form method="post" action="/clients/{escape(name)}/files" enctype="multipart/form-data">
                    <label>Add a file <input type="file" name="file"/></label> <button>Upload file</button>
                  </form>
                  <form method="post" action="/clients/{escape(name)}/restricted">
                    <input type="hidden" name="restricted" value="{"false" if c["restricted"] else "true"}"/>
                    <span>{restricted_line if c["restricted"]
                           else "Not restricted: its runs may go to any provider in the route table."}</span>
                    <button>{"Lift the restriction" if c["restricted"] else "Mark as restricted"}</button>
                  </form>
                  <form method="post" action="/clients/{escape(name)}/retention">
                    <label>Keep material for <input type="number" name="days" min="1" size="5"
                      value="{c["retention_days"] or ""}"/> days</label>
                    <button>Save retention period</button> <span class="meta">Leave empty to keep all of it.</span>
                  </form>
                  <details><summary>Delete this client</summary>
                    <p>Deleting removes the folder and the content of each run for {escape(name)}: briefs, drafts,
                     scores, replies, model inputs and outputs. Run costs and the audit log stay. You cannot undo this.</p>
                    <form method="post" action="/clients/{escape(name)}/delete">
                      <label>Type {escape(name)} to confirm <input type="text" name="confirm" autocomplete="off"/></label>
                      <button class="reject">Delete {escape(name)}</button>
                    </form>
                  </details>
                </section>"""
            )
        return render("Clients", "clients", f"""
<h1>Clients</h1>
<p class="lede">Each client has its own folder, and a run reads its own client's files and no others.
 Set a retention period and the daily job removes files and run content older than it.</p>
<form method="post" action="/retention/apply"><button>Apply retention periods now</button></form>
{"".join(cards) or "<p>No client has a folder yet. Create one with <code>POST /clients</code>.</p>"}
""")

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
            f'<td><span class="s-{escape(r["status"])}">{escape(r["status"].replace("_", " "))}</span></td>'
            f'<td>${(r["cost_usd"] or 0):.2f}</td></tr>'
            for r in runs
        )
        filters = " ".join(
            f'<a href="/inspect{"?status=" + s if s else ""}">{label}</a>'
            for s, label in [("", "all"), ("waiting_for_approval", "waiting for approval"),
                             ("waiting_for_input", "waiting for input"), ("failed", "failed"), ("done", "done")]
        )
        table = (
            '<div class="tablewrap"><table><tr><th>Started (UTC)</th><th>Workflow</th><th>Client</th><th>Status</th>'
            f"<th>Cost</th></tr>{rows}</table></div>"
            if runs else "<p>No run matches this filter.</p>"
        )
        return render("Runs", "runs", f"""
<h1>Runs</h1>
<p class="lede">Open a run to see each step, its model calls and its cost.</p>
<p>Show: {filters}. The list holds the most recent 200.</p>
{table}
""")

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
                  {calls}{tools}{block("Step output", step["output"])}
                </section>"""
            )
        by_node = "".join(
            f"<tr><td>{escape(n['node'])}</td><td>{n['steps']}</td><td>{n['model_calls']}</td>"
            f"<td>{n['tokens']:,}</td><td>${n['cost_usd']:.4f}</td></tr>"
            for n in trace["cost_by_node"]
        )
        untraced = (
            f'<p class="meta">${trace["untraced_cost_usd"]:.4f} of this run\'s cost has no per-node record '
            "(the run predates tracing, or a call reported no cost).</p>"
            if trace["untraced_cost_usd"] > 0.00005 else ""
        )
        prompts = ", ".join(f"{escape(node)} {escape(v)}" for node, v in trace["prompt_versions"]) or "none"
        flags = "".join(f"<li>{escape(str(f))}</li>" for f in run["flags"] or [])
        return render(f"Run {run['id'][:8]}", "runs", f"""
<h1>{escape(run["workflow"])} <span class="s-{escape(run["status"])}">{escape(run["status"].replace("_", " "))}</span></h1>
<p class="meta">run <code>{escape(run["id"])}</code> · client {escape(run["client"])} · started {escape(run["created_at"])} ·
 updated {escape(run["updated_at"])}</p>
{f'<p class="error">{escape(run["error_reason"])}</p>' if run["error_reason"] else ""}
<p class="total">${(run["cost_usd"] or 0):.4f} <span class="meta">{(run["tokens_in"] or 0):,} tokens in,
 {(run["tokens_out"] or 0):,} out{" · a fallback model answered" if run["fallback_used"] else ""}</span></p>
<p class="meta">skill library <code>{escape((run["skill_library_version"] or "")[:12])}</code> · prompts: {prompts}</p>
{f'<ul class="notes">{flags}</ul>' if flags else ""}
{block("Run inputs", tracing.clip(run["inputs"]))}{block("Run outputs", tracing.clip(run["outputs"]))}
<h2>Cost by node</h2>
{f'<div class="tablewrap"><table><tr><th>Node</th><th>Steps</th><th>Model calls</th><th>Tokens</th><th>Cost</th></tr>{by_node}</table></div>' if by_node else '<p>This run has no recorded steps.</p>'}
{untraced}
<h2>Steps</h2>
{"".join(cards) or "<p>This run has no recorded steps.</p>"}
{block("Tool calls outside a step", trace["unplaced_tool_calls"])}
""")

    # --- cost and settings ------------------------------------------------------------
    def _cap_banner() -> str:
        cap = costs.current_cap(db, settings)
        if cap["state"] == "alert":
            return (f'<div class="banner" role="status">Spend this month is at {cap["used"]:.0%} of the '
                    f'${cap["cap_usd"]:.2f} cap.</div>')
        if cap["state"] == "stopped":
            return (f'<div class="banner stop" role="status">Spend has reached the ${cap["cap_usd"]:.2f} monthly cap. '
                    "The agent refuses runs from the mail watcher and from schedules. "
                    "Runs you start still go ahead.</div>")
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
            return (f'<h2>{title}</h2><div class="tablewrap"><table><tr><th>Name</th><th>Runs</th><th>Failed</th>'
                    f"<th>Tokens</th><th>Cost</th></tr>{body}</table></div>")

        cap = s["cap"]
        if cap["state"] == "none":
            cap_line = "You have set no monthly cap (<code>monthly_cap_usd</code> in <code>config/budgets.yaml</code>)."
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
        mail = engine.harness.mail
        mail_line = (
            f"Mail goes through Microsoft Graph from {escape(mail.mailbox)}. <code>send_mail</code> sends a reply once you approve it."
            if mail else
            "No mail connection yet: <code>send_mail</code> holds approved replies in the local outbox."
        )
        cal = settings.calendar
        call_times = (
            f"{plural(cal.call_minutes, 'minute')}, {escape(cal.day_start)} to {escape(cal.day_end)} "
            f"{escape(cal.timezone)}, at least {plural(cal.earliest_days_ahead, 'day')} ahead; "
            + (f"free time read from {escape(mail.calendar)}" if mail else "no calendar connection, so replies ask for dates")
        )
        policy = settings.sponsorship
        sponsorship = (
            "no policy file: each enquiry escalates" if policy is None else
            f"pursue at {policy.pursue_at}, decline below {policy.decline_below}, escalate above "
            + ("any amount" if policy.escalate_above is None else f"{policy.escalate_above:,.0f} naira")
        )
        return render("Settings and cost", "cost", f"""
<h1>Settings and cost</h1>
{_cap_banner() if s["month"] == costs.month_bounds()[0] else ""}
<p class="month"><a href="/cost?month={before}">&larr; {before}</a> <strong>{s["month"]}</strong>
 <a href="/cost?month={after}">{after} &rarr;</a></p>
<p class="total">${s["total_usd"]:.2f} <span class="meta">across {plural(s["runs"], "run")}</span></p>
<p>{cap_line}</p>
{table("By workflow", s["by_workflow"])}
{table("By client", s["by_client"])}
<h2>Model routes</h2>
<div class="tablewrap"><table><tr><th>Tier</th><th>Primary</th><th>Fallback</th></tr>{routes}</table></div>
<h2>Ceilings for each run</h2>
<table>
<tr><td>Cost</td><td>${b.max_cost_usd:.2f}</td></tr>
<tr><td>Tokens</td><td>{b.max_total_tokens:,}</td></tr>
<tr><td>Tool calls</td><td>{b.max_tool_calls}</td></tr>
<tr><td>Elapsed time</td><td>{b.max_elapsed_seconds} seconds</td></tr>
</table>
<h2>Connected tools</h2>
<div class="tablewrap"><table><tr><th>Tool</th><th>Class</th><th>Approval</th></tr>{tools}</table></div>
<p class="meta">{mail_line}</p>
<h2>Other settings</h2>
<table>
<tr><td>Sponsorship policy</td><td>{sponsorship}</td></tr>
<tr><td>Call times</td><td>{call_times}</td></tr>
<tr><td>Mail watcher webhook</td><td>{"on" if settings.n8n_secret else "off (no shared secret set)"}</td></tr>
<tr><td>Creative review</td><td>images up to {settings.creative_review.image_max_edge} px,
 {settings.creative_review.pages_per_group} pages a call, {settings.creative_review.max_pages} pages at most</td></tr>
<tr><td>Approved for confidential work</td><td>{escape(", ".join(settings.confidential_providers)) or "no provider yet"}
 <span class="meta">(<code>config/providers.yaml</code>; mark restricted clients on the <a href="/files">Clients</a> screen)</span></td></tr>
<tr><td>Retention per client</td><td>Set it per client on the <a href="/files">Clients</a> screen.</td></tr>
</table>
<p class="meta">The agent reads these from <code>config/</code> and the server environment.
 Change them there. This screen shows them and edits none.</p>
""")

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
                      <button class="approve">Accept and change the skill file</button>
                    </form>
                    <form method="post" action="/learning/proposals/{p["id"]}">
                      <input type="hidden" name="decision" value="declined"/>
                      <input type="text" name="note" placeholder="reason (optional)" aria-label="Reason for declining"/>
                      <button class="reject">Decline</button>
                    </form>
                  </div>
                </section>"""
            )
        pending = "".join(cards) or "<p>No proposal waits for your decision.</p>"
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
              <div class="tablewrap"><table><tr><th>Workflow</th><th>Runs</th><th>Failed</th><th>Edit rate</th>
              <th>Rejection rate</th><th>Cost</th></tr>{rows}</table></div>"""
        files = "".join(
            f"<details><summary>{escape(name)}</summary><pre>{escape(text)}</pre></details>"
            for name, text in sorted(library.items())
        )
        return render("Skill library", "library", f"""
<h1>Skill library</h1>
<p class="lede">The agent cannot edit these files. The learning review proposes a change here, and it takes
 effect once you accept it.</p>
<p class="meta">library version <code>{version[:12]}</code></p>
<form method="post" action="/learning/reviews"><button>Run a learning review now</button></form>
<h2>Proposed changes</h2>
{pending}
{report}
<h2>Skills</h2>
{files}
""")

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
            notes = f'<ul class="notes say">{notes}</ul>' if notes else ""
            to = f'<p class="meta say">To: {escape(content["to"])}</p>' if content.get("to") else ""
            approve = {
                "send": "Approve and send this reply",
                "acknowledge": "Acknowledge, send nothing",
                "invite": "Approve and send this invitation",
                "move": "Approve and move this call",
                "cancel": "Approve and cancel this call",
                "outline": "Approve outline and start the draft",
                "save": "Approve and save this document",
            }.get(item.get("action"), "Approve and release this content")
            edit = (
                f"""<details><summary>Edit, then approve</summary>
                    <form method="post" action="/runs/{item["run_id"]}/approval" class="edit">
                      <input type="hidden" name="decision" value="approved"/>
                      <textarea name="edited_body" rows="16" aria-label="Your edited version">{escape(content.get("body", ""))}</textarea>
                      <button class="approve">Approve my edited version</button>
                    </form></details>"""
                if item.get("editable") else ""
            )
            rows.append(
                f"""
                <section class="card">
                  <h2 class="say">{escape(content.get("subject", ""))}</h2>
                  <p class="meta">run {item["run_id"]} · {escape(item["workflow"])} · client {escape(item["client"])}</p>
                  <button type="button" class="read" hidden>Read aloud</button>
                  {error}
                  {notes}
                  {to}
                  <pre class="say">{escape(content.get("body", ""))}</pre>
                  <p class="meta">Your approval covers this content: <code>{item["content_hash"][:16]}</code></p>
                  {edit}
                  <div class="actions">
                    <form method="post" action="/runs/{item["run_id"]}/approval">
                      <input type="hidden" name="decision" value="approved"/>
                      <button class="approve">{approve}</button>
                    </form>
                    <form method="post" action="/runs/{item["run_id"]}/approval">
                      <input type="hidden" name="decision" value="sent_back"/>
                      <input type="text" name="comment" placeholder="change to make" aria-label="Change to make"/>
                      <button class="sendback">Send back with a comment</button>
                    </form>
                    <form method="post" action="/runs/{item["run_id"]}/approval">
                      <input type="hidden" name="decision" value="rejected"/>
                      <input type="text" name="comment" placeholder="reason for rejecting" aria-label="Reason for rejecting"/>
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
                  <h2 class="say">Questions before drafting</h2>
                  <p class="meta">run {ask["run_id"]} · {escape(ask["workflow"])} · client {escape(ask["client"])}</p>
                  <button type="button" class="read" hidden>Read aloud</button>
                  <p class="say">{escape(ask["goal"])}</p>
                  <ol class="say">{questions}</ol>
                  <form method="post" action="/runs/{ask["run_id"]}/input" class="edit">
                    <textarea name="answers" rows="6" placeholder="Your answers" aria-label="Your answers"></textarea>
                    <button class="approve">Send answers and draft the outline</button>
                  </form>
                </section>"""
            )
        approvals, questions = len(engine.queue()), len(engine.input_requests())
        week_ago = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat(timespec="seconds")
        failed = sum(r["created_at"] >= week_ago for r in db.list_runs(status="failed"))
        listing = "".join(rows) or (
            "<p>No item needs your decision. New work appears here once a run reaches its approval step.</p>"
        )
        return render("Approval queue", "queue", f"""
<h1>Approval queue</h1>
<p class="lede">Each item below waits for your decision. You see the full content before you release it.</p>
{_cap_banner()}
<div class="strip">
  <a href="/queue"><strong>{approvals}</strong>{"approval" if approvals == 1 else "approvals"} waiting</a>
  <a href="/queue"><strong>{questions}</strong>{"run" if questions == 1 else "runs"} with questions for you</a>
  <a href="/inspect?status=failed"><strong>{failed}</strong>failed {"run" if failed == 1 else "runs"} in the last 7 days</a>
</div>
{listing}
""", script=READ_ALOUD_SCRIPT)

    return app


app = create_app()
