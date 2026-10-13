"""Web app and API.

The approval queue is the heart: every outbound action waits here, the exact
content is shown before anything can be released, and an approval matches the
content by hash. The pages are server-rendered inside the shell in ui.py,
which follows the owner's Chidimma OS screens.
"""

from __future__ import annotations

import hmac
import json
import os
import re
from datetime import datetime, timedelta, timezone
from html import escape
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import quote, urlsplit
from zoneinfo import ZoneInfo

from fastapi import BackgroundTasks, FastAPI, Header, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from personal_agent.api import dispatches, studio, ui, workspace
from personal_agent.api.ui import plural
from personal_agent.config import Settings, load_settings
from personal_agent.db import Database
from personal_agent import auth, connectors, costs, jobs, learning, retention, tracing
from personal_agent.engine import dispatch as agency_dispatch
from personal_agent.engine import skill_run, think
from personal_agent.engine.runner import Engine, UnknownWorkflow
from personal_agent.skills.loader import load_library
from personal_agent.skills import sync as skill_sync
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


class _Text(HTMLParser):
    """The words of an HTML mail, with a line break where a block ends. Scripts and styles are dropped."""

    BLOCKS = {"p", "div", "br", "li", "tr", "h1", "h2", "h3", "h4", "table", "blockquote"}

    def __init__(self):
        super().__init__()
        self.parts: list[str] = []
        self.skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style", "head"):
            self.skip += 1
        elif tag in self.BLOCKS:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in ("script", "style", "head"):
            self.skip = max(0, self.skip - 1)
        elif tag in self.BLOCKS:
            self.parts.append("\n")

    def handle_data(self, data):
        if not self.skip:
            self.parts.append(data)


def html_to_text(html: str) -> str:
    parser = _Text()
    parser.feed(html)
    lines = [" ".join(line.split()) for line in "".join(parser.parts).splitlines()]
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()


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
    var card = button.closest('dialog, .card');
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
  document.querySelectorAll('dialog').forEach(function (d) { d.addEventListener('close', stop); });
})();
</script>"""


# The review sheets. The server renders each one open, so a browser without
# scripts shows them down the page; with scripts they become modal dialogs
# opened from the list (or from a link that names one, as the home screen's do).
SHEET_SCRIPT = """<script>
(function () {
  var sheets = document.querySelectorAll('dialog.sheet');
  if (!sheets.length || !sheets[0].showModal) return;
  function show(id) {
    var sheet = document.getElementById(id);
    if (sheet && sheet.classList.contains('sheet') && !sheet.open) sheet.showModal();
  }
  sheets.forEach(function (sheet) {
    sheet.close();
    sheet.addEventListener('click', function (e) {
      if (e.target === sheet || e.target.closest('[data-close]')) sheet.close();
    });
    sheet.addEventListener('close', function () {
      if (location.hash === '#' + sheet.id) history.replaceState(null, '', location.pathname);
    });
  });
  document.querySelectorAll('[data-open]').forEach(function (link) {
    link.addEventListener('click', function (e) {
      e.preventDefault();
      history.replaceState(null, '', '#' + link.dataset.open);
      show(link.dataset.open);
    });
  });
  if (location.hash) show(location.hash.slice(1));
})();
</script>"""

# The home screen's bar. The question travels to the Think screen in the browser's
# own session storage, so client detail never lands in a URL or a server log.
DOCK_SCRIPT = """<script>
(function () {
  var form = document.getElementById('ask'), field = document.getElementById('ask-goal');
  if (!form) return;
  var mode = 'ask', pills = document.querySelectorAll('.pills [data-mode]');
  function choose(next) {
    mode = next;
    pills.forEach(function (pill) {
      var on = pill.dataset.mode === mode;
      pill.setAttribute('aria-checked', on ? 'true' : 'false');
      if (on) field.placeholder = pill.dataset.hint;
    });
  }
  pills.forEach(function (pill) {
    pill.addEventListener('click', function () { choose(pill.dataset.mode); field.focus(); });
  });
  // "Take action": the line becomes the title of a new task, on this screen if it has the form.
  function newTask(text) {
    var box = document.querySelector('details.add input[name="title"]');
    if (!box) return false;
    box.closest('details').open = true;
    box.value = text;
    box.focus();
    return true;
  }
  try {
    if (sessionStorage.getItem('mode') === 'action' && sessionStorage.getItem('goal') !== null) {
      if (newTask(sessionStorage.getItem('goal'))) { sessionStorage.removeItem('goal'); sessionStorage.removeItem('mode'); }
    }
  } catch (e) {}
  form.addEventListener('submit', function (e) {
    if (mode === 'action') {
      e.preventDefault();
      if (newTask(field.value)) { field.value = ''; return; }
    }
    try { sessionStorage.setItem('goal', field.value); sessionStorage.setItem('mode', mode); } catch (err) {}
    if (mode === 'action') location.href = '/today';
  });
  document.addEventListener('keydown', function (e) {
    if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 'k') { e.preventDefault(); field.focus(); }
  });
})();
</script>"""
GOAL_SCRIPT = """<script>
(function () {
  var goal = null, mode = null;
  try {
    goal = sessionStorage.getItem('goal'); mode = sessionStorage.getItem('mode');
    sessionStorage.removeItem('goal'); sessionStorage.removeItem('mode');
  } catch (e) {}
  var field = document.querySelector('textarea[name="question"]'), pick = document.querySelector('select[name="mode"]');
  if (pick && mode && pick.querySelector('option[value="' + mode + '"]')) pick.value = mode;
  if (goal && field) { field.value = goal; field.focus(); }
})();
</script>"""


def create_app(settings: Settings | None = None, gateway: ModelGateway | None = None, mail=None) -> FastAPI:
    settings = settings or load_settings()
    db = Database(settings.database_url)
    engine = Engine(settings, db, gateway=gateway, mail=mail)

    app = FastAPI(title="Personal Agent", version="0.1.0 (foundation)")
    app.state.engine = engine
    app.state.mailbox_check = []
    # The owner's workstreams, created once. One the owner later deletes stays deleted.
    ui.STREAM_COLOURS.update({w.name: w.colour for w in settings.workstreams if w.colour})
    if settings.workstreams and not db.audited_since("workstreams_seeded", ""):
        for stream in settings.workstreams:
            engine.harness.clients.ensure(stream.name)
        db.audit(None, "workstreams_seeded", {"names": [w.name for w in settings.workstreams]})

    # --- sign-in ----------------------------------------------------------------------
    # With the owner's email, password hash and session secret set, every route needs a session except the
    # health check, the sign-in page and the mail watcher's hook (which has its
    # own secret). With none set, a server refuses every request; a developer's
    # machine serves open.
    guard = (
        auth.LoginGuard(
            settings.owner_email, settings.owner_password_hash, settings.session_secret,
            totp_secret=settings.totp_secret, session_hours=settings.session_hours,
        )
        if settings.owner_email and settings.owner_password_hash and settings.session_secret
        else None
    )
    with_code = bool(guard and guard.totp_secret)

    @app.middleware("http")
    async def require_session(request: Request, call_next):
        path = request.url.path
        # The fonts are public files, and the sign-in page needs them before any session exists.
        # The icon is asked for before any sign-in, as the fonts are.
        if path in ("/health", "/login", "/favicon.ico") or path.startswith(("/hooks/", "/static/fonts/", "/static/brand/")):
            return await call_next(request)
        if guard is None:
            if settings.require_login:
                return PlainTextResponse(
                    "Sign-in is not set up on this server. Run `python -m personal_agent.auth` there "
                    "and add the lines it prints to .env.", status_code=503,
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
    def sign_in(next: str = "/"):
        if guard is None:
            return RedirectResponse("/", status_code=303)
        return ui.sign_in_page(settings.brand_name, next_path=next, with_code=with_code)

    @app.post("/login")
    async def sign_in_submit(request: Request):
        if guard is None:
            return RedirectResponse("/", status_code=303)
        form = await request.form()
        target = str(form.get("next") or "/")
        if not target.startswith("/") or target.startswith("//"):
            target = "/"  # never redirect off this site
        if guard.locked():
            return HTMLResponse(ui.sign_in_page(
                settings.brand_name, "Five failed attempts locked sign-in. Try again in 15 minutes.", target, with_code
            ), status_code=429)
        if not guard.check(str(form.get("email", "")), str(form.get("password", "")), str(form.get("code", ""))):
            db.audit(None, "login_failed", {"locked": guard.locked()})
            return HTMLResponse(ui.sign_in_page(
                settings.brand_name, "The email or the password did not match. Try again.", target, with_code
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

    def _zone() -> ZoneInfo:
        return ZoneInfo(settings.calendar.timezone)

    def render(title: str, active: str, body: str, script: str = "", refresh: int = 0, dock: str = "") -> str:
        agency_dispatch.release_due(engine)
        waiting = len(db.list_runs(status="waiting_for_approval")) + len(db.list_runs(status="waiting_for_input"))
        today = datetime.now(_zone())
        return ui.page(
            title, active, body, brand=settings.brand_name, waiting=waiting, today_count=ws_api.open_count(),
            signed_in=guard is not None, script=script, refresh=refresh, dock=dock,
            clients=engine.harness.clients.list_clients(), today=f"{today:%A}, {today.day} {today:%B %Y}",
        )

    app.mount("/static", StaticFiles(directory=Path(__file__).parent / "static"), name="static")

    @app.get("/favicon.ico", include_in_schema=False)
    def favicon():
        return RedirectResponse("/static/brand/favicon-32.png", status_code=308)

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

    @app.post("/hooks/n8n/outlook", status_code=201)
    async def outlook_mail(request: Request, x_agent_secret: str | None = Header(default=None)) -> dict:
        """The same hook, fed a message as Microsoft Graph describes it, so the watcher needs
        no field mapping: id, subject, from.emailAddress, body.content, receivedDateTime."""
        if not settings.n8n_secret:
            raise HTTPException(503, "the n8n webhook is not configured")
        if not x_agent_secret or not hmac.compare_digest(x_agent_secret, settings.n8n_secret):
            db.audit(None, "webhook_refused", {"hook": "n8n/outlook"})
            raise HTTPException(401, "bad or missing secret")
        try:
            message = await request.json()
            sender = ((message.get("from") or message.get("sender") or {}).get("emailAddress")) or {}
            content = message.get("body") or {}
            text = content.get("content") or message.get("bodyPreview") or ""
            if str(content.get("contentType", "")).lower() == "html" or "<html" in text[:400].lower():
                text = html_to_text(text)
            mail = InboundMail(
                message_id=message.get("id") or "", sender=sender.get("address") or "",
                sender_name=(sender.get("name") or None), subject=(message.get("subject") or "")[:1000],
                body=text.strip()[:200_000] or "(no text in the mail)", received_at=message.get("receivedDateTime"),
            )
        except Exception as e:  # not a mail the agent can read: refuse it, start nothing
            raise HTTPException(422, f"this is not an Outlook message the agent can read: {type(e).__name__}")
        return sponsorship_mail(mail, x_agent_secret)

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

    def _back_to(request: Request, path: str):
        """A browser that posted a form goes back to its screen; an API client gets the result."""
        if "text/html" in request.headers.get("accept", ""):
            return RedirectResponse(path, status_code=303)
        return None

    def _back_to_review(request: Request):
        return _back_to(request, "/queue")

    @app.get("/approvals/pending")
    def pending() -> list[dict]:
        return engine.queue()

    @app.post("/runs/{run_id}/approval")
    async def decide(run_id: str, request: Request):
        # Accepts JSON (API clients) and form-encoded posts (the review sheet's buttons).
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
            subject = engine.pending_approval(run_id)["content"].get("subject")
        except Exception:  # not waiting, or unknown: approve() below reports it
            subject = None
        try:
            result = engine.approve(run_id, body.decision, body.comment, body.edited_body)
        except KeyError:
            raise HTTPException(404, "run not found")
        except ValueError as e:
            raise HTTPException(409, str(e))
        run = db.get_run(run_id)
        decision_id = db.record_decision(
            run["client"], subject or _kind(run["workflow"]),
            {"approved": "approved", "sent_back": "changes", "rejected": "declined"}[body.decision],
            body.comment, "run", run_id,
        )
        # An approved agency brief has its own log, with the recall on it.
        sent_to = (run["outputs"] or {}).get("dispatch_id") if run["workflow"] == agency_dispatch.WORKFLOW else None
        if sent_to and body.decision == "approved":
            return _back_to(request, f"/dispatch/{sent_to}") or result
        return _back_to(request, f"/queue?done={decision_id}") or result

    @app.get("/inputs/pending")
    def pending_inputs() -> list[dict]:
        return engine.input_requests()

    @app.post("/runs/{run_id}/input")
    async def give_input(run_id: str, request: Request):
        # The owner's answers to a run that asked questions and is waiting.
        if request.headers.get("content-type", "").startswith("application/json"):
            body = OwnerInput(**(await request.json()))
        else:
            form = await request.form()
            body = OwnerInput(answers=str(form.get("answers", "")).replace("\r\n", "\n"))
        try:
            result = engine.provide_input(run_id, body.answers)
        except KeyError:
            raise HTTPException(404, "run not found")
        except ValueError as e:
            raise HTTPException(409, str(e))
        return _back_to_review(request) or result

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

    # --- new work ---------------------------------------------------------------------
    KEY_NAMES = {"openai": "OPENAI_API_KEY", "anthropic": "ANTHROPIC_API_KEY", "gemini": "GEMINI_API_KEY",
                 "groq": "GROQ_API_KEY", "openrouter": "OPENROUTER_API_KEY",
                 "ollama": "OLLAMA_API_BASE", "ollama_chat": "OLLAMA_API_BASE"}

    def _missing_model_keys() -> list[str]:
        """Names of the provider keys the route table needs and the server lacks."""
        needed = {KEY_NAMES.get(route.primary.split("/", 1)[0].lower()) for route in settings.route_table.values()}
        return sorted(name for name in needed if name and not os.environ.get(name))

    @app.get("/new", response_class=HTMLResponse)
    def new_work_page(notice: str = "", skill: str = "") -> str:
        names = engine.harness.clients.list_clients()
        options = "".join(f'<option value="{escape(n)}">{escape(n)}</option>' for n in names)
        client_fields = f"""
            <label>Client
              <select name="client_existing"><option value="">Choose a client</option>{options}</select></label>
            <label><span class="or">or a new client's name</span>
              <input type="text" name="client_new" maxlength="80" autocomplete="off"/></label>"""
        missing = _missing_model_keys()
        no_key = (
            f'<p class="banner" role="status">The server has no {escape(" or ".join(missing))} yet, so a run stops at '
            "its first model step. Add the key to <code>.env</code> on the server and restart.</p>" if missing else ""
        )
        return render("Agent work", "work", f"""
<p class="crumb"><a href="/work">Work</a> / Agent work</p>
<h1>Agent work</h1>
<p class="banner" role="status">To send an approved brief to an agency, use <a href="/dispatch">Agency dispatch</a>.</p>
<p class="lede">Give the agent a piece of work. It prepares a draft and puts it in Review for your decision.
 Sponsorship enquiries arrive by mail and need nothing from you here.</p>
{f'<div class="banner" role="alert">{escape(notice)}</div>' if notice else ""}
{no_key}
<section class="card" id="brief">
  <h2>Evaluate a brief</h2>
  <p>The agent scores the brief against your rubric, quotes its evidence, and drafts the questions to send back.</p>
  <form method="post" action="/new" enctype="multipart/form-data" class="stack">
    <input type="hidden" name="workflow" value="brief-evaluation"/>{client_fields}
    <label>The brief (PDF, Word or text file) <input type="file" name="brief_file"/></label>
    <label><span class="or">or paste the brief</span> <textarea name="brief_text" rows="6"></textarea></label>
    <button class="approve">Evaluate this brief</button>
  </form>
</section>
<section class="card" id="creative">
  <h2>Review creative work</h2>
  <p>The agent rates the work against its brief, page by page, and writes feedback for the person who made it.</p>
  <form method="post" action="/new" enctype="multipart/form-data" class="stack">
    <input type="hidden" name="workflow" value="creative-review"/>{client_fields}
    <label>The work (PDF or image files; export a deck as PDF) <input type="file" name="work_files" multiple/></label>
    <label>The brief it answers <input type="file" name="brief_file"/></label>
    <label><span class="or">or paste the brief</span> <textarea name="brief_text" rows="4"></textarea></label>
    <label>Feedback is for
      <select name="discipline"><option value="designer">a designer</option><option value="copywriter">a copywriter</option></select></label>
    <label>Kind of work (for example outdoor, social, deck) <input type="text" name="work_kind" maxlength="40"/></label>
    <button class="approve">Review this work</button>
  </form>
</section>
<section class="card" id="skill">
  <h2>Run a skill</h2>
  <p>A repeatable piece of work, done to its own written rubric. The agent works from your request and the material you
   give it, and reads no web page. The result comes to Review; approved, it is saved to the workstream's folder.</p>
  <form method="post" action="/new" enctype="multipart/form-data" class="stack">
    <input type="hidden" name="workflow" value="skill"/>
    <label>Skill <select name="skill">{"".join(
        f'<option value="{name}"{" selected" if name == skill else ""}>{escape(label)}</option>'
        for name, label in skill_run.SKILLS.items())}</select></label>{client_fields}
    <label>Your request <textarea name="request" rows="3" maxlength="8000" required></textarea></label>
    <label>Material <span class="or">paste the plan, the copy, the coverage, the notes. Task Prioritisation and
      Follow-up Management also read this workstream's open tasks.</span>
      <textarea name="material" rows="6" maxlength="60000"></textarea></label>
    <label>Or files (PDF, Word or text; for a logo critique, the logo as PNG, JPG or PDF)
      <input type="file" name="source_files" multiple/></label>
    <button class="approve">Run this skill</button>
  </form>
</section>
<section class="card" id="draft">
  <h2>Draft a document</h2>
  <p>The agent asks what it needs, proposes an outline for you to approve, then writes and checks the draft.</p>
  <form method="post" action="/new" enctype="multipart/form-data" class="stack">
    <input type="hidden" name="workflow" value="strategy-drafting"/>{client_fields}
    <label>The goal, in your words <textarea name="goal" rows="4" required></textarea></label>
    <label>Kind of document
      <select name="document_type"><option value="strategy">Strategy</option><option value="brief">Brief</option>
        <option value="memo">Approval memo</option></select></label>
    <label>Reader (for example the MD, the agency) <input type="text" name="audience" maxlength="120"/></label>
    <label>Source documents (optional; with none, the agent reads the client's folder)
      <input type="file" name="source_files" multiple/></label>
    <button class="approve">Start this draft</button>
  </form>
</section>
""")

    @app.post("/new")
    async def start_new_work(request: Request, background: BackgroundTasks):
        form = await request.form()

        def back(reason: str):
            return RedirectResponse(f"/new?notice={quote(reason)}", status_code=303)

        workflow = str(form.get("workflow") or "")
        name = str(form.get("client_new") or "").strip() or str(form.get("client_existing") or "").strip()
        if not name:
            return back("Choose a client, or type a new client's name.")
        try:
            scope = engine.harness.clients.ensure(name)
        except Exception as e:  # a name the folder rules refuse
            return back(str(e))

        async def stored(field: str) -> list[str] | str:
            """Save the uploaded files of one field into the client's folder; their names, or a refusal."""
            saved = []
            for upload in form.getlist(field):
                if not getattr(upload, "filename", ""):
                    continue
                data = await upload.read()
                if len(data) > 20 * 1024 * 1024:
                    return f"{upload.filename} is larger than 20 MB."
                try:
                    saved.append(engine.harness.clients.store_file(scope, upload.filename, data))
                except Exception as e:
                    return f"{upload.filename} could not be saved: {e}"
            return saved

        text = lambda field: str(form.get(field) or "").strip()  # noqa: E731
        brief_files = await stored("brief_file")
        if isinstance(brief_files, str):
            return back(brief_files)
        brief = {"brief_path": brief_files[0]} if brief_files else {"brief_text": text("brief_text")}
        if workflow == "brief-evaluation":
            if not brief_files and not text("brief_text"):
                return back("Add the brief as a file, or paste it.")
            inputs = brief
        elif workflow == "creative-review":
            work = await stored("work_files")
            if isinstance(work, str):
                return back(work)
            if not work:
                return back("Add the creative work as a PDF or image file.")
            if not brief_files and not text("brief_text"):
                return back("Add the brief the work answers, as a file or pasted text.")
            inputs = {
                **brief, "work_paths": work,
                "discipline": text("discipline") if text("discipline") in ("designer", "copywriter") else "designer",
                "work_kind": text("work_kind") or "general",
            }
        elif workflow == "strategy-drafting":
            if not text("goal"):
                return back("Say what the document is for.")
            sources = await stored("source_files")
            if isinstance(sources, str):
                return back(sources)
            inputs = {
                "goal": text("goal"),
                "document_type": text("document_type") if text("document_type") in ("strategy", "brief", "memo") else "strategy",
                "audience": text("audience") or None, "source_paths": sources,
            }
        elif workflow == "skill":
            if text("skill") not in skill_run.SKILLS:
                return back("Choose a skill from the list.")
            if not text("request"):
                return back("Say what you want done.")
            sources = await stored("source_files")
            if isinstance(sources, str):
                return back(sources)
            if text("skill") in skill_run.VISION_SKILLS and not sources:
                return back("Add the logo as a PNG, a JPG or a PDF. The agent has to see it.")
            if not sources and not text("material") and text("skill") not in skill_run.TASK_SKILLS:
                return back("Give the agent material to work from: paste it, or add a file.")
            workflow = text("skill")
            inputs = {"request": text("request"), "material": text("material").replace("\r\n", "\n")[:60_000],
                      "source_paths": sources}
        else:
            return back("That kind of work is not one the agent does.")

        # Record the run and answer at once; the agent works on it after the page has loaded.
        run_id = engine.start_run(workflow, scope.name, inputs, work=False)
        background.add_task(engine.work, run_id)
        return RedirectResponse(f"/inspect/{run_id}", status_code=303)

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
        working = run["status"] in ("queued", "running")
        state_line = (
            '<p class="banner" role="status">The agent is working on this. The page updates itself.</p>' if working else
            '<p class="banner" role="status">This is waiting for you in the <a href="/queue">Review</a>.</p>'
            if run["status"] in ("waiting_for_approval", "waiting_for_input") else ""
        )
        return render(f"Run {run['id'][:8]}", "runs", refresh=4 if working else 0, body=f"""
<h1>{escape(run["workflow"])} <span class="s-{escape(run["status"])}">{escape(run["status"].replace("_", " "))}</span></h1>
<p class="meta">run <code>{escape(run["id"])}</code> · client {escape(run["client"])} · started {escape(run["created_at"])} ·
 updated {escape(run["updated_at"])}</p>
{state_line}
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

    # --- model routes -----------------------------------------------------------------
    MODEL_ID = re.compile(r"[a-z0-9_]+/[A-Za-z0-9._:/-]{1,120}")

    def _key_for(model: str) -> str | None:
        """The setting this model needs and the server lacks; None when it is there."""
        known = next((m.key for m in settings.model_catalogue if m.id == model), None)
        name = known or KEY_NAMES.get(model.split("/", 1)[0].lower())
        return name if name and not os.environ.get(name) else None

    def _why_no_answer(model: str, detail: str) -> str:
        """A provider's refusal in the owner's terms, with the next step."""
        provider, low = model.split("/", 1)[0], detail.lower()
        if "no credits" in low or "insufficient_quota" in low or "exceeded your current quota" in low or "billing" in low:
            return (f"{provider} turned the call down: the account this key belongs to has no API credit. "
                    "Add credit in that provider's billing page, wait a few minutes, and test again. "
                    "A chat subscription is separate from API credit.")
        if "authentication" in low or "invalid_api_key" in low or "incorrect api key" in low or "invalid x-api-key" in low:
            return f"{provider} did not accept the key on the server. Save it again with ./deploy/setup.sh --key {provider}"
        if "notfound" in low or "does not exist" in low or "model_not_found" in low or "not_found" in low:
            return f"{provider} has no model by that name, or this account may not use it. Choose another model."
        return detail

    def _models_section(notice: str = "", tested: str = "", tested_tier: str = "", bad: bool = False) -> str:
        def options(current: str | None, none: bool = False) -> str:
            out = ['<option value="">None</option>'] if none else []
            listed = False
            for m in settings.model_catalogue:
                listed = listed or m.id == current
                lacks = _key_for(m.id)
                out.append(
                    f'<option value="{escape(m.id)}"{" selected" if m.id == current else ""}>{escape(m.label)}'
                    f'{" · open-weight" if m.open else ""}{f" · needs {lacks}" if lacks else ""}</option>'
                )
            if current and not listed:
                out.append(f'<option value="{escape(current)}" selected>{escape(current)}</option>')
            return "".join(out)

        uses = {"cheap": "Routine steps: sorting, extracting, short checks", "standard": "Most drafting and scoring",
                "strong": "The hardest steps: long documents, final checks",
                "vision": "Any step that shows the model a picture: creative review, logo critique"}
        forms = "".join(
            f"""<form method="post" action="/models/route" class="route">
  <input type="hidden" name="tier" value="{escape(tier)}"/>
  <div><h3>{escape(tier.capitalize())}</h3><p class="meta">{uses.get(tier, "")}</p>
    <p class="meta">Now: <code>{escape(route.primary)}</code>{f" · needs {_key_for(route.primary)}" if _key_for(route.primary) else ""}</p></div>
  <label>Model <select name="primary">{options(tested if tier == tested_tier and MODEL_ID.fullmatch(tested) else route.primary)}</select></label>
  <label>or another model's id <input type="text" name="primary_other" placeholder="provider/model" maxlength="130" autocomplete="off"/></label>
  <label>If that provider fails <select name="fallback">{options(route.fallback, none=True)}</select></label>
  <span class="actions"><button class="approve">Use for {escape(tier)}</button>
    <button formaction="/models/test" formnovalidate>Test this model</button></span>
</form>"""
            for tier, route in settings.route_table.items()
        )
        return f"""<h2 id="models">Models</h2>
<p class="lede">Each step of the agent's work belongs to one of these tiers. Choose the model each tier uses. Test a model
 before you rely on it: the test makes one small call and reports the reply.</p>
{f'<div class="banner{" stop" if bad else ""}" role="status">{escape(notice)}</div>' if notice else ""}
<section class="card">{forms}</section>"""

    def _chosen_model(form) -> str:
        return str(form.get("primary_other") or "").strip() or str(form.get("primary") or "").strip()

    def _models_notice(text: str):
        return RedirectResponse(f"/cost?notice={quote(text)}#models", status_code=303)

    @app.post("/models/route")
    async def set_model_route(request: Request):
        form = await request.form()
        tier, primary = str(form.get("tier") or ""), _chosen_model(form)
        fallback = str(form.get("fallback") or "").strip() or None
        if tier not in settings.route_table:
            raise HTTPException(400, "no such tier")
        for model in filter(None, (primary, fallback)):
            if not MODEL_ID.fullmatch(model):
                return _models_notice(f"'{model}' is not a model id. An id looks like provider/model.")
        if not primary:
            return _models_notice("Choose a model, or type its id.")
        if fallback == primary:
            fallback = None
        engine.set_route(tier, primary, fallback)
        lacks = _key_for(primary)
        return _models_notice(
            f"The {tier} tier now uses {primary}."
            + (f" The server has no {lacks} yet, so its steps stop until you add it." if lacks else "")
        )

    @app.post("/models/test")
    async def test_model(request: Request):
        form = await request.form()
        model, tier = _chosen_model(form), str(form.get("tier") or "")
        if not MODEL_ID.fullmatch(model):
            return _models_notice("Choose a model to test, or type its id as provider/model.")
        now = settings.route_table[tier].primary if tier in settings.route_table else ""
        saved = "" if model == now or not now else f" Nothing was saved: the {tier} tier still uses {now}."

        def report(text: str, bad: bool):
            # The tested model stays chosen in its row, so the next press acts on it.
            return RedirectResponse(
                f"/cost?notice={quote(text)}&tested={quote(model, safe='')}&tier={quote(tier)}{'&bad=1' if bad else ''}#models",
                status_code=303,
            )

        lacks = _key_for(model)
        if lacks:
            how = {"OLLAMA_API_BASE": "./deploy/setup.sh --local-model " + model.split("/", 1)[1]}.get(
                lacks, "./deploy/setup.sh --key " + model.split("/", 1)[0])
            return report(f"{model} was not called: the server has no {lacks}. Add it on the server with {how}.{saved}", True)
        result = engine.harness.gateway.probe(model)
        db.audit(None, "model_tested", {"model": model, "ok": result["ok"]})
        if result["ok"]:
            press = f" Press Use for {tier} to route the tier to it." if saved else ""
            return report(f"{model} answered: {result['detail']}.{press}", False)
        return report(f"{model} did not answer. {_why_no_answer(model, result['detail'])}{saved}", True)

    # --- mailbox ------------------------------------------------------------------------
    @app.post("/mailbox/test")
    def test_mailbox(request: Request):
        """Check the Microsoft connection step by step. It sends no mail."""
        mail = engine.harness.mail
        if mail is None:
            raise HTTPException(409, "the mailbox is not connected")
        steps = mail.check()
        db.audit(None, "mailbox_tested", {"ok": all(s["ok"] for s in steps)})
        app.state.mailbox_check = steps
        return _back_to(request, "/cost#mailbox") or {"steps": steps}

    @app.post("/mailbox/test-send")
    def test_send(request: Request):
        """One fixed mail from the mailbox to itself, at the owner's press of the button: the proof that sending works."""
        mail = engine.harness.mail
        if mail is None:
            raise HTTPException(409, "the mailbox is not connected")
        try:
            mail.send_reply(mail.mailbox, f"{settings.brand_name}: test mail",
                            "This is a test from your agent. It shows that the mailbox can send. No reply is needed.")
            step = {"step": "Send a test mail", "ok": True, "detail": f"Microsoft accepted it. Look for it in {mail.mailbox}"}
        except Exception as e:  # refused, or the outcome is unknown: both are reported, neither is retried
            step = {"step": "Send a test mail", "ok": False, "detail": f"{e}. Mail.Send (Application) needs admin consent"}
        db.audit(None, "mailbox_test_sent", {"ok": step["ok"]})
        app.state.mailbox_check = [step]
        return _back_to(request, "/cost#mailbox") or {"steps": [step]}

    @app.get("/cost", response_class=HTMLResponse)
    def cost_page(month: str | None = None, notice: str = "", tested: str = "", tier: str = "", bad: str = "") -> str:
        mailbox_check = app.state.mailbox_check
        app.state.mailbox_check = []  # shown once, on the page the test returns to
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
        tools = "".join(
            f"<tr><td>{escape(name)}</td><td>{escape(cls)}</td>"
            f"<td>{'owner approval for every call' if cls == 'release' else 'none'}</td></tr>"
            for name, cls in engine.harness.tools.registered()
        )
        plugins = "".join(
            f"""<tr><td><strong>{escape(c["label"])}</strong><br/><span class="meta">{escape(c["state"])}.</span></td><td>"""
            + "<br/>".join(f"<code>{escape(name)}</code> ({cls}): {escape(does)}" for name, cls, does in c["tools"])
            + "</td></tr>"
            for c in connectors.status(db)
        )
        def job_line(last: dict | None) -> str:
            if last is None:
                return "Has not run yet"
            word = '<span class="s-done">worked</span>' if last["ok"] else '<span class="s-failed">failed</span>'
            return f'{word} {escape(last["time"].replace("T", " ").replace("Z", " UTC"))}<br/><span class="meta">{escape(last["detail"])}</span>'

        scheduled = "".join(
            f"<tr><td>{escape(j['label'])}</td><td>{escape(j['schedule'])}</td><td>{job_line(j['last'])}</td></tr>"
            for j in jobs.last_runs(settings.clients_dir.parent / "jobs")
        )
        mail = engine.harness.mail
        lacking = [name for name, value in (
            ("MS_GRAPH_TENANT_ID", settings.graph_tenant_id), ("MS_GRAPH_CLIENT_ID", settings.graph_client_id),
            ("MS_GRAPH_CLIENT_SECRET", settings.graph_client_secret), ("MS_GRAPH_MAILBOX", settings.graph_mailbox),
        ) if not value]
        checked = "".join(
            f"<tr><td>{'<span class=\"s-done\">worked</span>' if step['ok'] else '<span class=\"s-failed\">failed</span>'}</td>"
            f"<td>{escape(step['step'])}</td><td>{escape(step['detail'])}</td></tr>"
            for step in mailbox_check
        )
        mailbox_section = (
            f"""<p>Connected to <strong>{escape(mail.mailbox)}</strong>. Free time is read from {escape(mail.calendar)}.</p>
<div class="actions"><form method="post" action="/mailbox/test"><button>Test the connection</button></form>
  <form method="post" action="/mailbox/test-send"><button>Send a test mail to {escape(mail.mailbox)}</button></form></div>
<p class="meta">The test reads the mailbox and the calendar, makes one draft and removes it. It sends nothing.
 The test mail goes from the mailbox to itself.</p>"""
            if mail else
            f"""<p class="banner" role="status">Not connected: the server has no {escape(" or ".join(lacking)) or "mailbox settings"}.
 Approved mail waits in the outbox on this server.</p>
<p class="meta">Register the application in Microsoft Entra (<code>deploy/microsoft-graph.md</code>, steps 1 to 4),
 then run <code>./deploy/setup.sh --mailbox</code> on the server. It asks for the four values and tests the connection.</p>"""
        ) + (f'<div class="tablewrap"><table><tr><th>Result</th><th>Step</th><th>What happened</th></tr>{checked}</table></div>'
             if checked else "")
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
{_models_section(notice, tested, tier, bool(bad))}
<h2>Ceilings for each run</h2>
<table>
<tr><td>Cost</td><td>${b.max_cost_usd:.2f}</td></tr>
<tr><td>Tokens</td><td>{b.max_total_tokens:,}</td></tr>
<tr><td>Tool calls</td><td>{b.max_tool_calls}</td></tr>
<tr><td>Elapsed time</td><td>{b.max_elapsed_seconds} seconds</td></tr>
</table>
<h2 id="jobs">Scheduled jobs</h2>
<div class="tablewrap"><table><tr><th>Job</th><th>Server time (UTC)</th><th>Last run</th></tr>{scheduled}</table></div>
<p class="meta">The server runs these by itself. Backups stay on the server for 14 days. For an encrypted copy off the server, run
 <code>./deploy/setup.sh --offsite</code>; the backup's line above then says the copy was sent and read back. <code>./deploy/restore.sh</code> on the server returns the agent to a backup.</p>
<h2 id="connectors">Connectors</h2>
<p class="lede">Outside creative services. You use them on the <a href="/studio">Studio</a> screen; what they make is saved
 to the workstream's folder or stays in your own Canva account.</p>
<div class="tablewrap"><table><tr><th>Service</th><th>Tools it will offer</th></tr>{plugins}</table></div>
<h2 id="mailbox">Mailbox and calendar</h2>
{mailbox_section}
<h2>Tools</h2>
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

    @app.post("/skills/{skill}/shipped")
    async def resolve_shipped_skill(skill: str, request: Request):
        """A skill the owner changed, whose shipped text has changed too: keep theirs, or take the shipped one."""
        shipped = settings.shipped_skills_dir
        if not shipped or skill not in {d["skill"] for d in skill_sync.diverged(shipped, settings.skills_dir)}:
            raise HTTPException(404, "no shipped update waits for that skill")
        take = (await _fields(request)).get("choice") == "shipped"
        skill_sync.resolve(shipped, settings.skills_dir, skill, take_shipped=take)
        version = engine.reload_library()
        db.audit(None, "skill_shipped_update_decided", {"skill": skill, "took_shipped": take, "library_version": version})
        return _back_to(request, "/library") or {"skill": skill, "took_shipped": take, "library_version": version}

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
    def library_page(ws: str = "") -> str:
        ws = ws_api.known(ws)
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
        updates = "".join(
            f"""<section class="card">
  <h2>{escape(d["skill"])}</h2>
  <p>An update to the app changed these rules, and you have changed them too. The agent kept yours.
   The lines below show what the update changes in the rules as they first shipped.</p>
  <pre>{escape(learning._diff(d["baseline"], d["shipped"]))}</pre>
  <div class="actions">
    <form method="post" action="/skills/{escape(d["skill"])}/shipped">
      <input type="hidden" name="choice" value="mine"/><button class="approve">Keep my rules</button></form>
    <form method="post" action="/skills/{escape(d["skill"])}/shipped">
      <input type="hidden" name="choice" value="shipped"/><button class="reject">Replace mine with the update</button></form>
  </div>
</section>"""
            for d in (skill_sync.diverged(settings.shipped_skills_dir, settings.skills_dir) if settings.shipped_skills_dir else [])
        )
        updates = f"<h2>Updates that met your changes</h2>{updates}" if updates else ""
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
        return render("Memory & decisions", "library", f"""
<p class="crumb">{escape(settings.brand_name)} / Memory</p>
<h1>Memory &amp; decisions</h1>
<p class="lede">Remember the decision. Remember why.</p>
{ws_api.ws_chips("/library", ws)}
<nav class="tabs" aria-label="Sections"><a href="#decisions">Decisions</a><a href="#context">Context</a>
 <a href="#drafts">Drafts</a><a href="#rules">Working rules</a><a href="#activity">Activity</a></nav>
{ws_api.memory_sections(ws)}
<h2 id="rules">Working rules</h2>
<p class="lede">The agent cannot edit these files. The learning review proposes a change here, and it takes
 effect once you accept it. An accepted change stays through every update to the app.</p>
<p class="meta">library version <code>{version[:12]}</code></p>
<form method="post" action="/learning/reviews"><button>Run a learning review now</button></form>
{updates}
<h2>Proposed changes</h2>
{pending}
{report}
<h2>Skills</h2>
{files}
{ws_api.activity()}
""", dock=_dock())

    # --- home -------------------------------------------------------------------------
    TAGS = {
        "brief-evaluation": ("Brief", "c-blue"), "strategy-drafting": ("Strategy", "c-purple"),
        "creative-review": ("Creative", "c-rose"), "sponsorship-triage": ("Sponsorship", "c-amber"),
        "call-change": ("Calls", "c-emerald"), "think": ("Think", "c-purple"),
        "agency-dispatch": ("Dispatch", "c-emerald"), "studio": ("Studio", "c-purple"),
        **{name: (label, "c-purple") for name, label in skill_run.SKILLS.items()},
    }
    STATUS_WORDS = {
        "waiting_for_approval": "waiting for you", "waiting_for_input": "has questions for you",
        "done": "done", "failed": "failed", "cancelled": "cancelled", "running": "working", "queued": "queued",
    }

    # --- the command bar ----------------------------------------------------------------
    # Six things to do with a line of text. Five go to Think, each asking for a different
    # kind of answer; "Take action" makes it a task, which the owner then works or approves.
    DOCK_MODES = (
        ("ask", "Ask", "spark", "Ask {name} anything"), ("research", "Research", "memory", "What should the agent research?"),
        ("create", "Create", "think", "What should the agent write?"), ("analyse", "Analyse", "intelligence", "What should the agent analyse?"),
        ("plan", "Plan", "runs", "What needs a plan?"), ("action", "Take action", "decide", "What needs doing? It becomes a task."),
    )

    def _dock() -> str:
        icon = ui.icon
        missing = _missing_model_keys()
        key_state = f"No {' or '.join(missing)} on the server yet" if missing else "Model key set"
        modes = "".join(
            f'<button type="button" role="radio" aria-checked="{"true" if key == "ask" else "false"}" data-mode="{key}" '
            f'data-hint="{escape(hint.format(name=settings.owner_name))}">{icon(glyph)}{label}</button>'
            for key, label, glyph, hint in DOCK_MODES
        )
        return f"""<div class="dock">
  <p class="assure">{icon("lock")}Your judgement stays in control. Each outside action waits for your review.</p>
  <form action="/think" method="get" id="ask">
    <label for="ask-goal" style="position:absolute;left:-9999px">Your request</label>
    <input type="text" id="ask-goal" placeholder="Ask {escape(settings.owner_name)} anything" autocomplete="off" maxlength="2000"/>
    <span class="kbd">Ctrl K</span>
    <a class="aside" href="/library#context" aria-label="Add to memory">{icon("memory")}</a>
    <button aria-label="Send">{icon("up")}</button>
  </form>
  <div class="pills" role="radiogroup" aria-label="What to do with it">{modes}
    <span class="state">{escape(key_state)}</span>
  </div>
</div>{DOCK_SCRIPT}"""

    @app.get("/", response_class=HTMLResponse)
    def home() -> str:
        icon = ui.icon
        hour = datetime.now(_zone()).hour
        part = "morning" if hour < 12 else "afternoon" if hour < 17 else "evening"
        open_items = [i for i in ws_api.items() if i["status"] != "done"]
        waiting = [i for i in open_items if i["status"] == "awaiting_approval"]
        at_risk = [i for i in open_items if i["risk"] == "high"]
        # What waits for the owner comes first, then what is at risk, then by time due.
        ranked = sorted(open_items, key=lambda i: (i["status"] != "awaiting_approval", i["risk"] != "high"))
        signals = ws_api.signal_list()
        reputation = sum(s["kind"] == "reputation" for s in signals)
        moment = datetime.now(timezone.utc).isoformat(timespec="minutes")
        ahead = sorted(
            (c for c in db.list_calls() if c["status"] == "scheduled" and c["start"] > moment), key=lambda c: c["start"]
        )

        def link(item: dict) -> str:
            # A task's own sheet lives on the Work screen.
            return f'/work{item["href"]}' if item["href"].startswith("#") else item["href"]

        if ranked:
            first = ranked[0]
            why = ("Open it to read it and decide." if first["status"] == "awaiting_approval"
                   else "It is at risk." if first["risk"] == "high" else "It is next in line.")
            view = (first["title"], f"For {first['ws']}. {why}", link(first))
        else:
            view = ("Nothing is waiting on you.", "Add a task, or give the agent work, and it shows here.", "/work")
        # What the owner decided today stays in view, struck through, as the Stitch screens show it.
        since, _ = _day_bounds()
        decided = "".join(
            f'<li><a href="/library#decisions"><span class="lead"><span class="num c-emerald">{icon("check")}</span>'
            f'<span><b class="struck">{escape(d["title"] or "")}</b><small>{escape(d["workstream"] or "")}</small></span></span>'
            f'<span class="end">{ws_api.chip(workspace.DECISIONS.get(d["decision"], (d["decision"], "c-slate")))}</span></a></li>'
            for d in db.list_decisions()[:3] if (d["created_at"] or "") >= since
        )

        def priority(n: int, item: dict) -> str:
            label, colour = item["tag"] or workspace.STATUSES[item["status"]]
            return (
                f'<li><a href="{link(item)}"><span class="lead"><span class="num {colour}">{n}</span>'
                f'<span><b>{escape(item["title"])}</b><small>{escape(item["ws"])}</small></span></span>'
                f'<span class="end"><span class="tag {colour}">{escape(label)}</span>{icon("chevron")}</span></a></li>'
            )

        priorities = "".join(priority(n, item) for n, item in enumerate(ranked[:5], 1)) + decided
        calls = "".join(
            f'<li><a href="/calendar"><span class="lead"><span><b>{escape(c["subject"] or "Call")}</b>'
            f'<small>{escape(c["label"])} · with {escape(c["sender"])}</small></span></span>'
            f'<span class="end">{icon("chevron")}</span></a></li>'
            for c in ahead[:5]
        )
        watch = "".join(
            f'<li><a href="/intelligence"><span class="lead"><span><b>{escape(s["title"])}</b>'
            f'<small>{escape(s["workstream"])}</small></span></span>'
            f'<span class="end">{ws_api.chip(workspace.SIGNALS.get(s["kind"], (s["kind"], "c-slate")))}</span></a></li>'
            for s in signals[:4]
        )
        connected = engine.harness.mail is not None
        return render("Home", "home", f"""
<h1>Good {part}, {escape(settings.owner_name)}.</h1>
<p class="lede">Here's what matters today.</p>
{_cap_banner()}
<a class="hero{"" if ranked else " clear"}" href="{view[2]}">
  <span class="lead"><span class="disc {"purple" if ranked else "emerald"}">{icon("spark" if ranked else "check")}</span>
    <span><span class="myview">{"My view" if ranked else "All caught up"}</span><strong>{escape(view[0])}</strong><small>{escape(view[1])}</small></span></span>
  <span class="go">{icon("chevron")}</span>
</a>
<div class="metrics">
  <a class="metric" href="/queue"><span class="lead"><span class="disc rose">{icon("decide")}</span>
    <span><b>{len(waiting)}</b><span>{"Decision" if len(waiting) == 1 else "Decisions"}</span><small>Need your input</small></span></span>{icon("chevron")}</a>
  <a class="metric" href="/today?f=risk"><span class="lead"><span class="disc amber">{icon("risk")}</span>
    <span><b>{len(at_risk)}</b><span>{"Item" if len(at_risk) == 1 else "Items"} at risk</span><small>Need attention</small></span></span>{icon("chevron")}</a>
  <a class="metric" href="/intelligence?kind=reputation"><span class="lead"><span class="disc indigo">{icon("intelligence")}</span>
    <span><b>{reputation}</b><span>Reputation {"signal" if reputation == 1 else "signals"}</span><small>On your watchlist</small></span></span>{icon("chevron")}</a>
</div>
<div class="columns">
  <section class="card">
    <div class="linkrow"><h2>Top priorities</h2><a href="/today">View all {icon("arrow")}</a></div>
    {f'<ul class="rows">{priorities}</ul>' if priorities else '<p class="quiet">No open item. Add a task on Today.</p>'}
  </section>
  <section class="card">
    <div class="linkrow"><h2>Calls ahead</h2><a href="/calendar">Calendar {icon("arrow")}</a></div>
    {"" if connected else '<p class="eyebrow">Calendar not connected</p>'}
    {f'<ul class="rows">{calls}</ul>' if calls else '<p class="quiet">No call is scheduled.</p>'}
  </section>
  <section class="card">
    <div class="linkrow"><h2>Intelligence</h2><a href="/intelligence">View all {icon("arrow")}</a></div>
    <p class="eyebrow">{f'Your watchlist · {ws_api.incoming_count()} new from your sources' if db.list_watch_sources() else "Your watchlist · feed not connected"}</p>
    {f'<ul class="rows">{watch}</ul>' if watch else '<p class="quiet">No signal on the watchlist yet.</p>'}
  </section>
</div>
""", dock=_dock())

    # --- today ------------------------------------------------------------------------
    def _day_bounds() -> tuple[str, str]:
        """The owner's day, as the UTC timestamps the tables hold."""
        start = datetime.now(_zone()).replace(hour=0, minute=0, second=0, microsecond=0)
        as_utc = lambda moment: moment.astimezone(timezone.utc).isoformat(timespec="seconds")  # noqa: E731
        return as_utc(start), as_utc(start + timedelta(days=1))

    def _calls_today() -> list[dict]:
        since, until = _day_bounds()
        return sorted(
            (c for c in db.list_calls() if c["status"] == "scheduled" and since[:16] <= c["start"][:16] < until[:16]),
            key=lambda c: c["start"],
        )

    def _rows(items: list[tuple[str, str, str, str]], empty: str) -> str:
        """A card's list: (link, title, line beneath, status for the dot or '')."""
        if not items:
            return f'<p class="quiet">{empty}</p>'
        return '<ul class="rows">' + "".join(
            f'<li><a href="{href}"><span class="lead">'
            + (f'<span class="dot {escape(dot)}"></span>' if dot else "")
            + f'<span><b>{escape(title)}</b><small>{escape(line)}</small></span></span>'
            f'<span class="end">{ui.icon("chevron")}</span></a></li>'
            for href, title, line, dot in items
        ) + "</ul>"

    def _kind(workflow: str) -> str:
        return TAGS.get(workflow, (workflow, ""))[0]

    # --- think ------------------------------------------------------------------------
    NO_CLIENT = "General"
    DEPTH_WORDS = {"quick": "Quick", "considered": "Considered", "deep": "Deep"}
    MODE_WORDS = {"ask": "Ask: a direct answer", "research": "Research: what is known, what to check",
                  "create": "Create: a piece ready to use", "analyse": "Analyse: the finding and its evidence",
                  "plan": "Plan: steps, owners, risks"}

    # The fifteen repeatable workflows the owner names. Those with an agent workflow behind
    # them open it; the others start a Think question, which is what the agent can do for them today.
    SKILL_LIST = (
        ("Campaign Review", "/new?skill=campaign-review#skill"), ("Creative Review", "/new#creative"),
        ("Logo and Identity Critique", "/new?skill=logo-critique#skill"),
        ("Copy Review", "/new?skill=copy-review#skill"),
        ("Strategic Recommendation", "/new#draft"), ("Proposal Review", "/new?skill=proposal-review#skill"), ("Approval Memo", "/new#draft"),
        ("Executive Email", "/new?skill=executive-email#skill"), ("Reputation Assessment", "/new?skill=reputation-assessment#skill"),
        ("Media Summary", "/new?skill=media-summary#skill"), ("Competitor Analysis", "/new?skill=competitor-analysis#skill"),
        ("Marketing Brief", "/new#brief"), ("Executive Update", "/new?skill=executive-update#skill"),
        ("Research Synthesis", "/new?skill=research-synthesis#skill"),
        ("Task Prioritisation", "/new?skill=task-prioritisation#skill"),
        ("Follow-up Management", "/new?skill=follow-up-management#skill"),
    )
    STARTERS = ("What needs my attention in this workstream?", "Challenge this campaign idea.",
                "Help me prepare a strategic recommendation.")

    @app.get("/think", response_class=HTMLResponse)
    def think_page(notice: str = "", wait: str = "", start: str = "") -> str:
        names = [n for n in engine.harness.clients.list_clients() if n != NO_CLIENT]
        options = "".join(f'<option value="{escape(n)}">{escape(n)}</option>' for n in names)
        depths = "".join(
            f'<option value="{depth}"{" selected" if depth == "considered" else ""}>{word}: '
            f'{escape(settings.route_table[tier].primary) if tier in settings.route_table else "no model routed"}</option>'
            for depth, word in DEPTH_WORDS.items() for tier in [think.DEPTHS[depth]]
        )
        missing = _missing_model_keys()
        no_key = (
            f'<p class="banner" role="status">The server has no {escape(" or ".join(missing))} yet, so a question gets '
            'no answer. Add the key on the server, or choose another model on <a href="/cost#models">Settings and cost</a>.</p>'
            if missing else ""
        )
        past = [r for r in db.list_runs() if r["workflow"] == "think"][:20]
        cards = []
        for r in past:
            inputs, out = r["inputs"] or {}, r["outputs"] or {}
            if out.get("answer"):
                caveats = "".join(f"<li>{escape(str(c))}</li>" for c in out.get("caveats", []))
                body = f'<pre class="say">{escape(out["answer"])}</pre>' + (
                    f'<p class="eyebrow">Assumed or not checked</p><ul class="notes say">{caveats}</ul>' if caveats else ""
                )
            elif r["status"] == "failed":
                body = f'<p class="error">{escape(r["error_reason"] or "This run failed.")}</p>'
            elif inputs.get("question") is None:
                body = '<p class="quiet">The retention period removed this content.</p>'
            else:
                body = '<p class="quiet">The agent is thinking. This page updates itself.</p>'
            cards.append(f"""<section class="card" id="q-{r["id"]}">
  <p class="chips"><span class="tag c-blue">{escape(MODE_WORDS.get(inputs.get("mode"), "Ask").split(":")[0])}</span>
    <span class="tag c-purple">{escape(DEPTH_WORDS.get(inputs.get("depth"), "Considered"))}</span>
    <span class="tag c-slate">{escape(r["client"])}</span>
    <span class="s-{escape(r["status"])}">{escape(STATUS_WORDS.get(r["status"], r["status"]))}</span></p>
  <h3 class="say">{escape(str(inputs.get("question") or "Question removed"))}</h3>
  <button type="button" class="read" hidden>Read aloud</button>
  {body}
  <p class="meta"><a href="/inspect/{r["id"]}">How the agent got here</a> · ${r["cost_usd"] or 0:.4f}</p>
  {f'<form method="post" action="/think/{r["id"]}/save"><button>Save to memory</button></form>' if out.get("answer") else ""}
</section>""")
        waiting = any(r["id"] == wait and r["status"] in ("queued", "running") for r in past)
        starters = "".join(
            f'<a href="/think?start={quote(text)}">{escape(text)}</a>' for text in STARTERS
        )
        lens = "".join(f"<li>{escape(q)}</li>" for q in settings.core.lens)
        skill_links = "".join(
            f'<li><a href="{href or "/think?start=" + quote(name + ": ")}"><span class="lead"><span><b>{name}</b>'
            f'<small>{"Agent workflow" if href else "Starts a question here"}</small></span></span>'
            f'<span class="end">{ui.icon("chevron")}</span></a></li>'
            for name, href in SKILL_LIST
        )
        return render("Think", "think", f"""
<p class="crumb">{escape(settings.brand_name)} / Think</p>
<h1>Let's think this through.</h1>
<p class="lede">A quiet space for sharper thinking. The agent reasons from your question, the material you paste and what
 you have recorded about the workstream. It reads no web page and no file, and it sends nothing to anyone.</p>
{f'<div class="banner" role="alert">{escape(notice)}</div>' if notice else ""}
{no_key}
<div class="columns think">
<div>
<section class="card">
  <p class="eyebrow">A good place to start</p>
  <nav class="filters">{starters}</nav>
  <form method="post" action="/think" class="stack">
    <label>Your question <textarea name="question" rows="4" maxlength="8000" required>{escape(start[:500])}</textarea></label>
    <label>Material to reason from <span class="or">optional: paste a note, a mail, figures</span>
      <textarea name="material" rows="4" maxlength="100000"></textarea></label>
    <div class="row two">
      <label>Mode <select name="mode">{"".join(f'<option value="{k}">{escape(v)}</option>' for k, v in MODE_WORDS.items())}</select></label>
      <label>Depth, and the model that answers
        <select name="depth">{depths}</select></label>
    </div>
    <label>Context <span class="or">the agent reads this workstream's knowledge and no other's</span>
      <select name="client"><option value="">No workstream</option>{options}</select></label>
    <button class="approve">Ask this question</button>
  </form>
</section>
{"<h2>Answers</h2>" + "".join(cards) if cards else ""}
</div>
<aside>
<section class="card">
  <div class="linkrow"><h2>The {escape(settings.owner_name)} lens</h2></div>
  <p class="quiet">Good thinking should survive scrutiny. Each answer is tested against these.</p>
  {f'<ul class="notes">{lens}</ul>' if lens else ""}
</section>
<section class="card">
  <div class="linkrow"><h2>Skill library</h2><a href="/library#rules">The rules {ui.icon("arrow")}</a></div>
  <p class="quiet">{len(SKILL_LIST)} repeatable workflows. One shared standard.</p>
  <ul class="rows">{skill_links}</ul>
</section>
</aside>
</div>
""", script=GOAL_SCRIPT + READ_ALOUD_SCRIPT, refresh=4 if waiting else 0)

    @app.post("/think")
    async def ask(request: Request, background: BackgroundTasks):
        form = await request.form()
        question = str(form.get("question") or "").strip()
        if not question:
            return RedirectResponse(f"/think?notice={quote('Type a question first.')}", status_code=303)
        client = str(form.get("client") or "").strip() or NO_CLIENT
        if client != NO_CLIENT and client not in engine.harness.clients.list_clients():
            return RedirectResponse(f"/think?notice={quote('Choose a client from the list.')}", status_code=303)
        depth = str(form.get("depth") or "")
        inputs = {
            "question": question[:8000], "material": str(form.get("material") or "").replace("\r\n", "\n")[:100_000],
            "depth": depth if depth in think.DEPTHS else "considered",
            "mode": str(form.get("mode") or "") if str(form.get("mode") or "") in think.MODES else "ask",
        }
        try:
            run_id = engine.start_run("think", client, inputs, work=False)
        except costs.MonthlyCapReached as e:
            return RedirectResponse(f"/think?notice={quote(str(e))}", status_code=303)
        background.add_task(engine.work, run_id)
        return RedirectResponse(f"/think?wait={run_id}#q-{run_id}", status_code=303)

    # --- intelligence -----------------------------------------------------------------
    @app.get("/intelligence", response_class=HTMLResponse)
    def intelligence_page(ws: str = "", kind: str = "all") -> str:
        ws, kind = ws_api.known(ws), kind if kind in workspace.SIGNALS else "all"
        now = datetime.now(timezone.utc)
        # The end sits a minute ahead, so a decision made this second is inside the period.
        since = (now - timedelta(days=30)).isoformat(timespec="seconds")
        until = (now + timedelta(minutes=1)).isoformat(timespec="seconds")
        report = learning.weekly_report(db, since, until)
        total = lambda field: sum(r[field] for r in report.values())  # noqa: E731
        decisions = total("approved") + total("sent_back") + total("rejected")
        enquiries = [row for row in db.list_ledger() if (row["created_at"] or "") >= since]
        by_route: dict[str, int] = {}
        for row in enquiries:
            by_route[row["route"] or "not routed"] = by_route.get(row["route"] or "not routed", 0) + 1
        routes = ", ".join(f"{count} {escape(route.replace('_', ' '))}" for route, count in sorted(by_route.items())) or "none yet"
        proposals = db.list_proposals(status="pending")
        spend = costs.summary(db, settings)

        def share(part: int, whole: int) -> str:
            return f"{part} of {whole}" if whole else "none yet"

        table = "".join(
            f"<tr><td>{escape(_kind(name))}</td><td>{r['runs']}</td><td>{r['failed_runs']}</td><td>{r['approved']}</td>"
            f"<td>{r['edited']}</td><td>{r['sent_back']}</td><td>{r['rejected']}</td><td>${r['cost_usd']:.2f}</td></tr>"
            for name, r in sorted(report.items())
        )
        return render("Intelligence", "intelligence", f"""
<p class="crumb">{escape(settings.brand_name)} / Intelligence</p>
<h1>Intelligence</h1>
<p class="lede">What changed. Why it matters. What happens next.</p>
{ws_api.watchlist(ws, kind)}
<h2>From the agent's records</h2>
<p class="lede">The last 30 days: your decisions, the enquiries that came in, and the changes the agent proposes to its
 working rules.</p>
<div class="metrics">
  <a class="metric" href="/inspect"><span class="lead"><span class="disc rose">{ui.icon("decide")}</span>
    <span><b>{decisions}</b><span>{"Decision" if decisions == 1 else "Decisions"} you made</span>
    <small>Approved {share(total("approved"), decisions)}, edited first {total("edited")}</small></span></span>{ui.icon("chevron")}</a>
  <a class="metric" href="/inspect"><span class="lead"><span class="disc amber">{ui.icon("brief")}</span>
    <span><b>{len(enquiries)}</b><span>Sponsorship {"enquiry" if len(enquiries) == 1 else "enquiries"}</span>
    <small>{routes}</small></span></span>{ui.icon("chevron")}</a>
  <a class="metric" href="/library"><span class="lead"><span class="disc indigo">{ui.icon("memory")}</span>
    <span><b>{len(proposals)}</b><span>Proposed {"change" if len(proposals) == 1 else "changes"} to the rules</span>
    <small>From the weekly review, for you to accept or decline</small></span></span>{ui.icon("chevron")}</a>
</div>
<div class="columns one">
  <section class="card">
    <div class="linkrow"><h2>By kind of work</h2><a href="/cost">Cost in full {ui.icon("arrow")}</a></div>
    {f'<div class="tablewrap"><table><tr><th>Work</th><th>Runs</th><th>Failed</th><th>Approved</th><th>Edited</th><th>Sent back</th><th>Rejected</th><th>Cost</th></tr>{table}</table></div>'
      if table else '<p class="quiet">No run in the last 30 days.</p>'}
    <p class="meta">This month so far: ${spend["total_usd"]:.2f} across {plural(spend["runs"], "run")}.</p>
  </section>
</div>
""", dock=_dock())

    @app.get("/queue", response_class=HTMLResponse)
    def queue_page(ws: str = "", done: str = "") -> str:
        icon = ui.icon
        ws = ws_api.known(ws)
        made = next((d for d in db.list_decisions()[:20] if d["id"] == done), None) if done else None
        toast = (
            f"""<div class="toast" role="status"><span class="disc emerald">{icon("check")}</span>
  <span class="grow"><span class="eyebrow">{escape(workspace.DECISIONS.get(made["decision"], (made["decision"], ""))[0])} · recorded</span>
    <strong>{escape(made["title"] or "")}</strong>
    <small>{"Kept in Memory with your reason." if made["reason"] else "Kept in Memory. You gave no reason."}</small></span>
  <a href="/library#decisions">View in Memory</a></div>""" if made else ""
        )
        approvals = [a for a in engine.queue() if not ws or a["client"] == ws]
        asks = [a for a in engine.input_requests() if not ws or a["client"] == ws]
        task_items, task_sheets = ws_api.decision_sheets(ws)
        total = len(approvals) + len(asks) + len(task_items)
        mailbox = engine.harness.mail is not None

        def head(n: int, run: dict, title: str) -> str:
            label, colour = TAGS.get(run["workflow"], (run["workflow"], "c-slate"))
            return f"""<div class="sheet-strip"></div>
  <header class="sheet-head">
    {ui.stream_badge(run["client"])}
    <div class="grow">
      <p class="chips"><span class="tag c-emerald">{escape(run["client"])} workstream</span>
        <span class="tag c-rose">Decision {n} of {total}</span>
        <span class="tag {colour}">{escape(label)}</span></p>
      <h2 class="say" id="t-{run["run_id"]}">{escape(title)}</h2>
    </div>
    <button type="button" class="close" data-close aria-label="Close">{icon("x")}</button>
  </header>"""

        def context(text: str) -> str:
            return f'<p class="context">{icon("info")}<span><strong>Context:</strong> {escape(text)}</span></p>'

        rows, sheets = [], []
        for n, item in enumerate(approvals, 1):
            run_id, content = item["run_id"], item["content"]
            title = content.get("subject") or "Draft for your approval"
            approve, effect = {
                "send": ("Approve and send this reply", "Approve it and the agent sends this reply."),
                "acknowledge": ("Acknowledge, send nothing", "Acknowledge it and the agent sends nothing."),
                "invite": ("Approve and send this invitation", "Approve it and the agent sends this invitation."),
                "move": ("Approve and move this call", "Approve it and the agent moves this call."),
                "cancel": ("Approve and cancel this call", "Approve it and the agent cancels this call."),
                "outline": ("Approve outline and start the draft", "Approve the outline and the agent starts the draft."),
                "save": ("Approve and save this document", "Approve it and the agent saves this document."),
                "dispatch": ("Approve and dispatch to the agency",
                             "Approve it and the agent sends this mail to the agency once the hold ends. Until then you can recall it."),
                "halt": ("Approve and send this halt notice", "Approve it and the agent sends this notice to the agency at once."),
            }.get(item.get("action"), ("Approve and release this content", "Approve it and the agent releases this content."))
            if item.get("action") == "send" and not mailbox:
                effect += " No mailbox is connected yet, so the reply waits in the outbox."
            # Drafted content derives from untrusted material: always escaped.
            notes = "".join(f"<li>{escape(str(note))}</li>" for note in item.get("notes", []))
            evaluation = (
                f"""<section class="evaluation"><span class="mark">{icon("spark")}</span>
      <div><p class="eyebrow">{escape(settings.brand_name)} evaluation</p><ul class="notes say">{notes}</ul></div></section>"""
                if notes else ""
            )
            error = f'<p class="banner stop error" role="alert">{escape(item["error"])}</p>' if item.get("error") else ""
            to = f'<p class="meta say">To: {escape(content["to"])}</p>' if content.get("to") else ""
            edit = (
                f"""<details><summary>Edit, then approve</summary>
      <form method="post" action="/runs/{run_id}/approval" class="edit">
        <input type="hidden" name="decision" value="approved"/>
        <textarea name="edited_body" rows="16" aria-label="Your edited version">{escape(content.get("body", ""))}</textarea>
        <button class="approve">Approve my edited version</button>
      </form></details>"""
                if item.get("editable") else ""
            )
            sheets.append(f"""<dialog class="sheet" id="d-{run_id}" aria-labelledby="t-{run_id}" open>
  {head(n, item, title)}
  <div class="sheet-body">
    {context("The agent prepared this for " + item["client"] + ". " + effect)}
    {error}
    {evaluation}
    <div class="linkrow"><p class="eyebrow">The content you approve</p>
      <button type="button" class="read" hidden>Read aloud</button></div>
    {to}
    <pre class="say">{escape(content.get("body", ""))}</pre>
    {edit}
    <form method="post" action="/runs/{run_id}/approval" id="f-{run_id}" class="stack">
      <label class="eyebrow" for="c-{run_id}">Why this decision? The change to make, or your reason</label>
      <textarea id="c-{run_id}" name="comment" rows="2" maxlength="4000"></textarea>
    </form>
    <p class="meta">Your approval covers this content: <code>{item["content_hash"][:16]}</code> · run {run_id}</p>
  </div>
  <footer class="sheet-foot">
    <span class="actions">
      <button form="f-{run_id}" name="decision" value="sent_back" class="sendback">{icon("note")}Request revisions</button>
      <button form="f-{run_id}" name="decision" value="rejected" class="reject">Reject</button>
    </span>
    <button form="f-{run_id}" name="decision" value="approved" class="approve">{icon("check")}{approve}</button>
  </footer>
</dialog>""")
            label, colour = TAGS.get(item["workflow"], (item["workflow"], "c-slate"))
            rows.append((run_id, title, item["client"], label, colour))

        for n, ask in enumerate(asks, len(approvals) + 1):
            run_id = ask["run_id"]
            questions = "".join(f"<li>{escape(str(q))}</li>" for q in ask["questions"])
            sheets.append(f"""<dialog class="sheet" id="d-{run_id}" aria-labelledby="t-{run_id}" open>
  {head(n, ask, "Questions before drafting")}
  <div class="sheet-body">
    {context("The agent needs your answers before it drafts the outline for " + ask["client"] + ".")}
    <div class="linkrow"><p class="eyebrow">Your goal</p><button type="button" class="read" hidden>Read aloud</button></div>
    <p class="say">{escape(ask["goal"])}</p>
    <p class="eyebrow">The questions</p>
    <ol class="say">{questions}</ol>
    <form method="post" action="/runs/{run_id}/input" id="f-{run_id}" class="stack">
      <label class="eyebrow" for="c-{run_id}">Your answers</label>
      <textarea id="c-{run_id}" name="answers" rows="6" required></textarea>
    </form>
    <p class="meta">run {run_id}</p>
  </div>
  <footer class="sheet-foot"><span></span>
    <button form="f-{run_id}" class="approve">{icon("check")}Send answers and draft the outline</button>
  </footer>
</dialog>""")
            label, colour = TAGS.get(ask["workflow"], (ask["workflow"], "c-slate"))
            rows.append((run_id, "Questions before drafting", ask["client"], label, colour))

        for item in task_items:
            rows.append((f"t{item['task']}", item["title"], item["ws"], "Task", "c-slate"))
        sheets.append(task_sheets)
        listing = "".join(
            f'<li><a href="#d-{run_id}" data-open="d-{run_id}"><span class="lead"><span class="num {colour}">{n}</span>'
            f'<span><b>{escape(title)}</b><small>{escape(client)}</small></span></span>'
            f'<span class="end"><span class="tag {colour}">{escape(label)}</span>{icon("chevron")}</span></a></li>'
            for n, (run_id, title, client, label, colour) in enumerate(rows, 1)
        )
        week_ago = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat(timespec="seconds")
        failed = sum(r["created_at"] >= week_ago for r in db.list_runs(status="failed"))
        waiting = (
            f'<section class="card"><div class="linkrow"><h2>{plural(len(rows), "decision")} pending</h2>'
            f'<span class="eyebrow">Review the evidence. Make the call. Keep the reason.</span></div><ul class="rows">{listing}</ul></section>'
            if rows else
            '<p>No item needs your decision. Give the agent work on the <a href="/new">Agent work</a> screen, or move a task '
            "to Awaiting approval; it comes here for your call.</p>"
        )
        return render("Review", "queue", f"""
<p class="crumb">{escape(settings.brand_name)} / Review</p>
<h1>Review</h1>
<p class="lede">Your judgement. With the context to make it count.</p>
{toast}
{_cap_banner()}
{ws_api.ws_chips("/queue", ws)}
<div class="strip">
  <a href="/queue"><strong>{len(approvals)}</strong>{"approval" if len(approvals) == 1 else "approvals"} waiting</a>
  <a href="/queue"><strong>{len(asks)}</strong>{"run" if len(asks) == 1 else "runs"} with questions for you</a>
  <a href="/inspect?status=failed"><strong>{failed}</strong>failed {"run" if failed == 1 else "runs"} in the last 7 days</a>
</div>
{waiting}
{"".join(sheets)}
""", script=SHEET_SCRIPT + READ_ALOUD_SCRIPT, dock=_dock())

    ws_api = workspace.install(
        app, settings=settings, db=db, engine=engine, render=render, tags=TAGS, status_words=STATUS_WORDS,
        sheet_script=SHEET_SCRIPT, cap_banner=_cap_banner, calls_today=_calls_today, fields=_fields, dock=_dock,
    )
    dispatches.install(app, settings=settings, db=db, engine=engine, render=render, ws_api=ws_api, dock=_dock)
    studio.install(app, settings=settings, db=db, engine=engine, render=render, ws_api=ws_api, status_words=STATUS_WORDS)
    return app


app = create_app()
