"""Agency dispatch screens, after the owner's Stitch designs: the form that
prepares a brief for an agency, the list of dispatches, and the log of one
dispatch with its recall.

The log shows what the system knows: who it went to, when, what was attached,
who approved it. It makes no claim about what the agency did with it.
"""

from __future__ import annotations

from datetime import datetime, timezone
from html import escape
from urllib.parse import quote
from zoneinfo import ZoneInfo

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse

from personal_agent.api import ui
from personal_agent.clients import ClientScope
from personal_agent.costs import MonthlyCapReached
from personal_agent.engine import dispatch

STATES = {
    "held": ("Held: you can recall it", "c-amber"), "sending": ("Sending", "c-blue"), "sent": ("Sent", "c-emerald"),
    "outbox": ("In the outbox: no mailbox connected", "c-amber"), "recalled": ("Recalled before sending", "c-rose"),
    "halt_pending": ("Halt notice waits for your approval", "c-amber"), "halted": ("Halt notice sent", "c-rose"),
    "failed": ("Not sent: it failed", "c-rose"), "uncertain": ("Outcome uncertain: check Sent Items", "c-rose"),
}


def install(app: FastAPI, *, settings, db, engine, render, ws_api, dock) -> None:
    icon = ui.icon
    zone = ZoneInfo(settings.calendar.timezone)

    def local(stamp: str | None) -> str:
        if not stamp:
            return ""
        when = datetime.fromisoformat(stamp).astimezone(zone)
        return f"{when.day} {when:%b}, {when:%H:%M}"

    def minutes_left(d: dict) -> int:
        left = (datetime.fromisoformat(d["release_at"]) - datetime.now(timezone.utc)).total_seconds()
        return max(0, int(left // 60) + (1 if left % 60 else 0))

    def state_chip(d: dict) -> str:
        return ws_api.chip(STATES.get(d["status"], (d["status"], "c-slate")))

    # --- the list, and the form ---------------------------------------------------------------
    @app.get("/dispatch", response_class=HTMLResponse)
    def dispatch_page(ws: str = "", revise: str = "", notice: str = "") -> str:
        ws = ws_api.known(ws)
        earlier = db.get_dispatch(revise) if revise else None
        if earlier:
            ws = earlier["workstream"]
        dispatch.release_due(engine)
        found = [d for d in db.list_dispatches() if not ws or d["workstream"] == ws]
        rows = "".join(
            f'<li><a href="/dispatch/{d["id"]}"><span class="lead">{ui.stream_badge(d["workstream"])}'
            f'<span><b>{escape(d["subject"] or "")}</b><small>{escape(d["workstream"])} · to {escape(d["agency"] or d["recipient"])}'
            f'{" · halt notice" if d["kind"] == "halt" else ""}</small></span></span>'
            f'<span class="end">{state_chip(d)}<span class="due">{escape(local(d["sent_at"] or d["release_at"]))}</span>{icon("chevron")}</span></a></li>'
            for d in found
        )
        streams = '<option value="">Choose a workstream</option>' + "".join(
            f'<option value="{escape(n)}"{" selected" if n == ws else ""}>{escape(n)}</option>'
            for n in engine.harness.clients.list_clients()
        )
        on_file = ""
        if ws:
            files = engine.harness.clients.list_files(ClientScope(ws))
            ticked = set((earlier or {}).get("attachments") or [])
            on_file = "".join(
                f'<label class="tick"><input type="checkbox" name="attach" value="{escape(f["path"])}"{" checked" if f["path"] in ticked else ""}'
                f'{" disabled" if f["size"] > dispatch.MAX_FILE_BYTES else ""}/> {escape(f["path"])} '
                f'<span class="or">{f["size"] / 1_000_000:.1f} MB{", over the 3 MB limit" if f["size"] > dispatch.MAX_FILE_BYTES else ""}</span></label>'
                for f in files
            )
        value = lambda key: escape((earlier or {}).get(key) or "")  # noqa: E731
        revised = (
            f"This replaces the brief sent on {local(earlier['sent_at'] or earlier['release_at'])}.\n\n{earlier['body']}"
            if earlier else ""
        )
        return render("Agency dispatch", "work", f"""
<p class="crumb"><a href="/work">Work</a> / Agency dispatch</p>
<h1>Agency dispatch</h1>
<p class="lede">Send an approved brief to an agency. You approve the exact mail first. It is then held for a short window,
 and you can recall it in that time. After it has gone, a recall sends the agency a halt notice.</p>
{f'<div class="banner" role="alert">{escape(notice)}</div>' if notice else ""}
{"" if engine.harness.mail else '<p class="banner" role="status">No mailbox is connected yet, so an approved brief goes to the outbox on this server and is not sent.</p>'}
{ws_api.ws_chips("/dispatch", ws)}
<details class="card add"{" open" if earlier or not found else ""}><summary>{icon("plus")}{"Revise and dispatch again" if earlier else "Dispatch a brief"}</summary>
  <form method="post" action="/dispatch" enctype="multipart/form-data" class="stack">
    <input type="hidden" name="revision_of" value="{escape(revise if earlier else "")}"/>
    <div class="row two">
      <label>Workstream <select name="workstream" required>{streams}</select></label>
      <label>Hold before sending, in minutes <input type="number" name="hold_minutes" min="0" max="{dispatch.MAX_HOLD_MINUTES}" value="{settings.dispatch_hold_minutes}"/></label>
    </div>
    <div class="row two">
      <label>Agency <input type="text" name="agency" maxlength="120" value="{value("agency")}" required/></label>
      <label>Their email address <input type="email" name="to" maxlength="320" value="{value("recipient")}" required/></label>
    </div>
    <label>Subject <input type="text" name="subject" maxlength="200" value="{escape(("Revised: " + earlier["subject"]) if earlier else "")}" required/></label>
    <label>Your direction <span class="or">optional: the route you approved, or what changed. It opens the mail.</span>
      <textarea name="directive" rows="2" maxlength="2000"></textarea></label>
    <label>The brief <textarea name="body" rows="10" maxlength="100000" required>{escape(revised)}</textarea></label>
    {f'<fieldset><legend>Attach from the {escape(ws)} folder</legend>{on_file}</fieldset>' if on_file else ""}
    <label>Attach new files <span class="or">each up to 3 MB; they are saved to the workstream's folder</span>
      <input type="file" name="files" multiple/></label>
    <button class="approve">Prepare for my approval</button>
  </form></details>
<section class="card">
  <div class="linkrow"><h2>Dispatches</h2></div>
  {f'<ul class="rows">{rows}</ul>' if rows else '<p class="quiet">No brief has been dispatched yet.</p>'}
</section>
""", dock=dock())

    @app.post("/dispatch")
    async def prepare_dispatch(request: Request):
        form = await request.form()
        text = lambda key, limit: str(form.get(key) or "").strip()[:limit]  # noqa: E731

        def back(reason: str):
            return RedirectResponse(f"/dispatch?notice={quote(reason)}", status_code=303)

        ws = ws_api.known(text("workstream", 80))
        if not ws:
            return back("Choose the workstream this brief belongs to.")
        if not dispatch.ADDRESS.fullmatch(text("to", 320)):
            return back("Give the agency's email address.")
        if not text("subject", 200) or not text("body", 100_000):
            return back("A brief needs a subject and its text.")
        scope = ClientScope(ws)
        attachments = [str(p) for p in form.getlist("attach")]
        for upload in form.getlist("files"):
            if not getattr(upload, "filename", ""):
                continue
            data = await upload.read()
            if len(data) > dispatch.MAX_FILE_BYTES:
                return back(f"{upload.filename} is over 3 MB. Put a link to it in the brief instead.")
            try:
                attachments.append(engine.harness.clients.store_file(scope, upload.filename, data))
            except Exception as e:
                return back(f"{upload.filename} could not be saved: {e}")
        try:
            hold = max(0, min(dispatch.MAX_HOLD_MINUTES, int(text("hold_minutes", 4) or settings.dispatch_hold_minutes)))
        except ValueError:
            hold = settings.dispatch_hold_minutes
        directive = text("directive", 2000).replace("\r\n", "\n")
        body = text("body", 100_000).replace("\r\n", "\n")
        inputs = {
            "kind": "brief", "agency": text("agency", 120), "to": text("to", 320), "subject": text("subject", 200),
            "body": (f"Direction: {directive}\n\n" if directive else "") + body,
            "attachments": list(dict.fromkeys(attachments)), "directive": directive or None, "hold_minutes": hold,
            "revision_of": text("revision_of", 64) or None,
        }
        try:
            run_id = engine.start_run(dispatch.WORKFLOW, ws, inputs)
        except MonthlyCapReached as e:
            return back(str(e))
        run = db.get_run(run_id)
        if run["status"] != "waiting_for_approval":
            return back(run["error_reason"] or "The brief could not be prepared.")
        return RedirectResponse(f"/queue#d-{run_id}", status_code=303)

    # --- one dispatch: its log ------------------------------------------------------------------
    def owned(dispatch_id: str) -> dict:
        d = db.get_dispatch(dispatch_id)
        if not d:
            raise HTTPException(404, "no such dispatch")
        return d

    @app.get("/dispatch/{dispatch_id}", response_class=HTMLResponse)
    def dispatch_log(dispatch_id: str, notice: str = "") -> str:
        dispatch.release_due(engine)  # so the log shows a brief whose window has just ended as it now stands
        d = owned(dispatch_id)
        held, left = d["status"] == "held", 0
        if held:
            left = minutes_left(d)
        sizes = {f["path"]: f["size"] for f in engine.harness.clients.list_files(ClientScope(d["workstream"]))} \
            if d["workstream"] in engine.harness.clients.list_clients() else {}
        files = "".join(
            f'<li><div class="row-static"><span class="lead"><span class="disc sm">{icon("brief")}</span>'
            f'<span><b>{escape(path)}</b><small>{f"{sizes[path] / 1_000_000:.1f} MB" if path in sizes else "no longer in the folder"}</small></span></span>'
            f'<span class="end"><span class="tag c-slate">Attached</span></span></div></li>'
            for path in d["attachments"] or []
        )
        time_line = {
            "held": f"Sends at {local(d['release_at'])}", "sent": f"Sent {local(d['sent_at'])}",
            "recalled": f"Recalled {local(d['recalled_at'])}. Nothing was sent.",
            "halted": f"Sent {local(d['sent_at'])}. Halt notice sent {local(d['recalled_at'])}.",
            "halt_pending": f"Sent {local(d['sent_at'])}. The halt notice waits in Review.",
            "outbox": "Approved. Held in the outbox on this server; no mailbox is connected.",
        }.get(d["status"], d["note"] or "")
        linked = lambda other, words: (  # noqa: E731
            f'<p class="meta">{words} <a href="/dispatch/{other["id"]}">{escape(other["subject"] or "")}</a>.</p>' if other else ""
        )
        replaced_by = next((x for x in db.list_dispatches() if x["revision_of"] == d["id"]), None)
        if held:
            action = (f'<form method="post" action="/dispatch/{d["id"]}/recall"><button class="recall">{icon("risk")}'
                      f'Emergency recall ({left} min remaining)</button></form>')
        elif d["status"] == "sent" and d["kind"] == "brief":
            action = (f'<form method="post" action="/dispatch/{d["id"]}/recall"><button class="recall">{icon("risk")}'
                      "Recall: prepare a halt notice</button></form>")
        elif d["status"] == "halt_pending":
            action = '<a class="button" href="/queue">Open the halt notice in Review</a>'
        else:
            action = ""
        again = (f'<a class="button approve" href="/dispatch?revise={d["id"]}">{icon("draft")}Revise and dispatch again</a>'
                 if d["kind"] == "brief" and d["status"] in ("recalled", "halted", "sent", "failed", "outbox") and not replaced_by else "")
        return render("Dispatch log", "work", f"""
<p class="crumb"><a href="/work">Work</a> / <a href="/dispatch">Agency dispatch</a> / Log</p>
{f'<div class="banner" role="status">{escape(notice)}</div>' if notice else ""}
<article class="sheet-page{" recalled" if d["status"] in ("recalled", "halted", "halt_pending", "failed", "uncertain") else ""}">
  <div class="sheet-strip"></div>
  <header class="sheet-head">
    {ui.stream_badge(d["workstream"])}
    <div class="grow"><p class="chips"><span class="tag c-emerald">{escape(d["workstream"])} workstream · {"halt notice" if d["kind"] == "halt" else "re-dispatch" if d["revision_of"] else "dispatch"} log</span>
      {state_chip(d)}<span class="tag c-slate">ID {escape(d["id"][:8])}</span></p>
      <h1>{escape(d["subject"] or "")}</h1></div>
  </header>
  <div class="sheet-body">
    <div class="facts">
      <div><p class="eyebrow">Recipient agency</p><b>{escape(d["agency"] or "")}</b><small>{escape(d["recipient"] or "")}</small></div>
      <div><p class="eyebrow">{"Re-dispatch" if d["revision_of"] else "Dispatch"} time</p><b>{escape(local(d["sent_at"]) or "Not sent")}</b><small>{escape(time_line)}</small></div>
      <div><p class="eyebrow">Signed off by</p><b>{escape(settings.owner_name)}</b><small>Approval <code>{escape((d["approved_hash"] or "")[:12])}</code></small></div>
    </div>
    {f'<section class="evaluation"><span class="mark">{icon("spark")}</span><div><p class="eyebrow">Your direction</p><p class="say">{escape(d["directive"])}</p></div></section>' if d["directive"] else ""}
    {linked(db.get_dispatch(d["revision_of"]) if d["revision_of"] else None, "This replaces")}
    {linked(replaced_by, "Replaced by")}
    {linked(db.get_dispatch(d["halts"]) if d["halts"] else None, "This halts")}
    <p class="eyebrow">The mail, as approved</p>
    <pre class="say">{escape(d["body"] or "")}</pre>
    <div class="linkrow"><p class="eyebrow">Files attached</p><span class="meta">{ui.plural(len(d["attachments"] or []), "file")}</span></div>
    {f'<ul class="rows">{files}</ul>' if files else '<p class="quiet">No file was attached.</p>'}
    <p class="meta">This log records what this system did. It cannot see whether the agency opened the mail or its files.</p>
  </div>
  <footer class="sheet-foot">
    <span class="actions"><a class="button" href="/dispatch/{d["id"]}/receipt">{icon("brief")}Download the audit receipt</a>
      <a class="button" href="/inspect/{d["run_id"]}">How it got here</a></span>
    <span class="actions">{action}{again}</span>
  </footer>
</article>
""", refresh=30 if held or d["status"] == "sending" else 0)

    @app.post("/dispatch/{dispatch_id}/recall")
    def recall_dispatch(dispatch_id: str):
        owned(dispatch_id)
        try:
            result = dispatch.recall(engine, dispatch_id)
        except ValueError as e:
            return RedirectResponse(f"/dispatch/{dispatch_id}?notice={quote(str(e).capitalize() + '.')}", status_code=303)
        if result.get("halt_run"):
            return RedirectResponse(f"/queue#d-{result['halt_run']}", status_code=303)
        return RedirectResponse(
            f"/dispatch/{dispatch_id}?notice={quote('Recalled. Nothing was sent to the agency.')}", status_code=303
        )

    @app.get("/dispatch/{dispatch_id}/receipt")
    def receipt(dispatch_id: str) -> PlainTextResponse:
        d = owned(dispatch_id)
        lines = [
            f"{settings.brand_name}: agency dispatch receipt", "",
            f"Dispatch      {d['id']}", f"Kind          {d['kind']}", f"Workstream    {d['workstream']}",
            f"Agency        {d['agency']}", f"Sent to       {d['recipient']}", f"Subject       {d['subject']}",
            f"Status        {STATES.get(d['status'], (d['status'],))[0]}",
            f"Approved by   {settings.owner_name}", f"Approval hash {d['approved_hash'] or ''}",
            f"Prepared      {d['created_at'] or ''} UTC", f"Release time  {d['release_at'] or ''} UTC",
            f"Sent          {d['sent_at'] or 'not sent'}", f"Recalled      {d['recalled_at'] or 'no'}",
            f"Replaces      {d['revision_of'] or 'nothing'}", f"Run           {d['run_id']}",
            "", "Files attached:", *([f"  {p}" for p in d["attachments"] or []] or ["  none"]),
            "", "The mail, as approved:", "", d["body"] or "",
        ]
        return PlainTextResponse(
            "\n".join(lines), headers={"content-disposition": f'attachment; filename="dispatch-{d["id"][:8]}.txt"'}
        )
