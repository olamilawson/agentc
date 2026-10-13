"""The owner's workspace, after the owner's own Chidimma OS prototype: tasks
across workstreams (Today, Work), decisions with their reasons, memory, the
watchlist and the Core screen.

Tasks, decisions, knowledge and signals are the owner's records. An agent run
shows beside them as an item of work, so one list holds what the owner is
doing and what the agent is doing. Nothing here is sample data: a list with
no entry says so.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from html import escape
from types import SimpleNamespace
from urllib.parse import quote
from zoneinfo import ZoneInfo

from fastapi import BackgroundTasks, FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from personal_agent import learning
from personal_agent.api import ui
from personal_agent.engine import signal_scan

STATUSES = {"todo": ("To do", "c-slate"), "in_progress": ("In progress", "c-blue"),
            "awaiting_approval": ("Awaiting approval", "c-amber"), "done": ("Done", "c-emerald")}
RISKS = {"high": ("High risk", "c-rose"), "medium": ("Medium risk", "c-amber"), "low": ("Low risk", "c-slate")}
KNOWLEDGE = {"priority": "Priority", "brand_rule": "Brand rule", "preference": "Preference", "lesson": "Lesson"}
SIGNALS = {"reputation": ("Reputation", "c-rose"), "regulation": ("Regulation", "c-blue"), "opportunity": ("Opportunity", "c-emerald")}
DECISIONS = {"approved": ("Approved", "c-emerald"), "changes": ("Changes requested", "c-amber"), "declined": ("Declined", "c-rose")}
FILTERS = (("open", "Open"), ("all", "All"), ("decisions", "Decisions"), ("risk", "At risk"), ("done", "Done"))
DUE = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}")
# Runs that are not items of work: a question on Think, and the agent's own weekly review.
NOT_WORK = {"think", "studio", "signal-scan", learning.WORKFLOW}


def install(app: FastAPI, *, settings, db, engine, render, tags, status_words, sheet_script, cap_banner,
            calls_today, fields, dock) -> SimpleNamespace:
    icon = ui.icon
    zone = ZoneInfo(settings.calendar.timezone)

    def clients() -> list[str]:
        return engine.harness.clients.list_clients()

    def known(ws: str) -> str | None:
        return ws if ws and ws in clients() else None

    def back(request_or_path, default: str) -> RedirectResponse:
        path = request_or_path if isinstance(request_or_path, str) else default
        if not path.startswith("/") or path.startswith("//"):
            path = default
        return RedirectResponse(path, status_code=303)

    def chip(pair: tuple[str, str]) -> str:
        return f'<span class="tag {pair[1]}">{escape(pair[0])}</span>'

    # --- items: the owner's tasks and the agent's runs, in one shape ---------------------
    def due_label(due: str | None) -> str:
        if not due or not DUE.fullmatch(due):
            return ""
        when, today = datetime.fromisoformat(due), datetime.now(zone).replace(tzinfo=None)
        days = (when.date() - today.date()).days
        if days == 0:
            return "Overdue" if when < today else f"{when:%H:%M}"
        if days < 0:
            return "Overdue"
        return "Tomorrow" if days == 1 else f"{when.day} {when:%b}"

    def items(ws: str | None = None) -> list[dict]:
        out = []
        for t in db.list_tasks():
            out.append({
                "key": f"t{t['id']}", "task": t["id"], "title": t["title"], "detail": t["detail"] or "",
                "ws": t["workstream"], "status": t["status"] if t["status"] in STATUSES else "todo",
                "risk": t["risk"] if t["risk"] in RISKS else None, "owner": t["owner"] or settings.owner_name,
                "due": t["due"], "tag": None,
                "href": f"/queue#d-t{t['id']}" if t["status"] == "awaiting_approval" else f"#e-{t['id']}",
            })
        subjects = {i["run_id"]: i["content"].get("subject") or "Draft for your approval" for i in engine.queue()}
        subjects.update({a["run_id"]: "Questions before drafting" for a in engine.input_requests()})
        week_ago = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat(timespec="seconds")
        for r in db.list_runs():
            if r["workflow"] in NOT_WORK or r["status"] == "cancelled":
                continue
            if r["status"] == "done" and (r["updated_at"] or r["created_at"]) < week_ago:
                continue  # finished work leaves the board after a week; the Runs screen keeps it
            waiting = r["status"] in ("waiting_for_approval", "waiting_for_input")
            label = tags.get(r["workflow"], (r["workflow"], "c-slate"))
            inputs = r["inputs"] or {}
            about = str(inputs.get("goal") or inputs.get("subject") or inputs.get("brief_path") or "")[:80]
            out.append({
                "key": r["id"], "task": None,
                "title": subjects.get(r["id"]) or (f"{label[0]}: {about}" if about else f"{label[0]} for {r['client']}"),
                "detail": (r["error_reason"] or "This run failed.")[:200] if r["status"] == "failed"
                          else f"The agent: {status_words.get(r['status'], r['status'])}",
                "ws": r["client"],
                "status": "awaiting_approval" if waiting else "done" if r["status"] == "done"
                          else "todo" if r["status"] == "failed" else "in_progress",
                "risk": "high" if r["status"] == "failed" else None, "owner": "Agent", "due": None, "tag": label,
                "href": f"/queue#d-{r['id']}" if waiting
                        else f"/dispatch/{(r['outputs'] or {}).get('dispatch_id')}" if (r["outputs"] or {}).get("dispatch_id")
                        else f"/inspect/{r['id']}",
            })
        if ws:
            out = [i for i in out if i["ws"] == ws]
        # Soonest first; what has no time set comes after what has.
        return sorted(out, key=lambda i: (i["due"] is None, i["due"] or "", i["risk"] != "high", i["title"].lower()))

    def filtered(found: list[dict], f: str) -> list[dict]:
        return [
            i for i in found
            if f == "all" or (f == "open" and i["status"] != "done") or (f == "done" and i["status"] == "done")
            or (f == "decisions" and i["status"] == "awaiting_approval")
            or (f == "risk" and i["risk"] == "high" and i["status"] != "done")
        ]

    def open_count() -> int:
        return sum(i["status"] != "done" for i in items())

    # --- shared pieces ---------------------------------------------------------------------
    def ws_chips(path: str, ws: str | None, **keep: str) -> str:
        """The row of workstreams above a screen; choosing one narrows the screen to it."""
        extra = "".join(f"&{k}={quote(v)}" for k, v in keep.items() if v)
        link = lambda name, label: (  # noqa: E731
            f'<a href="{path}?ws={quote(name)}{extra}"{" aria-current=true" if (ws or "") == name else ""}>{escape(label)}</a>'
        )
        return '<nav class="filters" aria-label="Workstreams">' + link("", "All workstreams") + "".join(
            link(name, name) for name in clients()
        ) + "</nav>"

    def tabs(path: str, ws: str | None, f: str, choices=FILTERS, name: str = "f") -> str:
        ws_part = f"&ws={quote(ws)}" if ws else ""
        return '<nav class="tabs" aria-label="Show">' + "".join(
            f'<a href="{path}?{name}={key}{ws_part}"{" aria-current=true" if key == f else ""}>{label}</a>'
            for key, label in choices
        ) + "</nav>"

    def opener(item: dict) -> str:
        """The link's attributes: a sheet on this page opens in place; any other link is followed."""
        sheet = item["href"][1:] if item["href"].startswith("#") else ""
        return f'href="{item["href"]}"' + (f' data-open="{sheet}"' if sheet else "")

    def sheet_head(dom_id: str, ws: str, chips: str, title: str) -> str:
        return f"""<div class="sheet-strip"></div>
  <header class="sheet-head">
    {ui.stream_badge(ws)}
    <div class="grow"><p class="chips"><span class="tag c-emerald">{escape(ws)} workstream</span>{chips}</p>
      <h2 class="say" id="t-{dom_id}">{escape(title)}</h2></div>
    <button type="button" class="close" data-close aria-label="Close">{icon("x")}</button>
  </header>"""

    def options(choices: dict, current: str | None, none: str = "") -> str:
        first = f'<option value="">{none}</option>' if none else ""
        return first + "".join(
            f'<option value="{key}"{" selected" if key == current else ""}>{escape(value[0] if isinstance(value, tuple) else value)}</option>'
            for key, value in choices.items()
        )

    def ws_options(current: str | None) -> str:
        return '<option value="">Choose a workstream</option>' + "".join(
            f'<option value="{escape(n)}"{" selected" if n == current else ""}>{escape(n)}</option>' for n in clients()
        )

    def new_task_form(path: str, ws: str | None) -> str:
        return f"""<details class="card add"><summary>{icon("plus")}New task</summary>
  <form method="post" action="/tasks" class="stack">
    <input type="hidden" name="back" value="{escape(path)}"/>
    <label>The task <input type="text" name="title" maxlength="200" required/></label>
    <label>Workstream <select name="workstream" required>{ws_options(ws)}</select></label>
    <label>What it needs <span class="or">optional</span><textarea name="detail" rows="2" maxlength="2000"></textarea></label>
    <div class="row">
      <label>Status <select name="status">{options(STATUSES, "todo")}</select></label>
      <label>Risk <select name="risk">{options(RISKS, None, "Not set")}</select></label>
      <label>Due <span class="or">{escape(settings.calendar.timezone)}</span><input type="datetime-local" name="due"/></label>
    </div>
    <button class="approve">Add this task</button>
  </form></details>"""

    def edit_sheets(found: list[dict], path: str) -> str:
        """One sheet for each of the owner's tasks shown: read it, change it, or remove it."""
        out = []
        for i in found:
            if not i["task"]:
                continue
            t = i["task"]
            decide = (
                f'<p class="context">{icon("info")}<span><strong>This waits for your decision.</strong> '
                f'<a href="/queue#d-t{t}">Open it in Review</a> to approve, request changes or decline, and to record the reason.</span></p>'
                if i["status"] == "awaiting_approval" else ""
            )
            out.append(f"""<dialog class="sheet" id="e-{t}" aria-labelledby="t-e-{t}" open>
  {sheet_head(f"e-{t}", i["ws"], chip(STATUSES[i["status"]]) + (chip(RISKS[i["risk"]]) if i["risk"] else ""), i["title"])}
  <div class="sheet-body">
    {decide}
    <form method="post" action="/tasks/{t}" id="f-e-{t}" class="stack">
      <input type="hidden" name="back" value="{escape(path)}"/>
      <label>The task <input type="text" name="title" maxlength="200" value="{escape(i["title"])}" required/></label>
      <label>What it needs <textarea name="detail" rows="3" maxlength="2000">{escape(i["detail"])}</textarea></label>
      <div class="row">
        <label>Status <select name="status">{options(STATUSES, i["status"])}</select></label>
        <label>Risk <select name="risk">{options(RISKS, i["risk"], "Not set")}</select></label>
        <label>Due <input type="datetime-local" name="due" value="{escape(i["due"] or "")}"/></label>
      </div>
    </form>
    <p class="meta">Owner: {escape(i["owner"])}</p>
  </div>
  <footer class="sheet-foot">
    <form method="post" action="/tasks/{t}/delete" class="actions"><input type="hidden" name="back" value="{escape(path)}"/>
      <button class="reject">Remove this task</button></form>
    <button form="f-e-{t}" class="approve">{icon("check")}Save changes</button>
  </footer>
</dialog>""")
        return "".join(out)

    def row(i: dict) -> str:
        chips = chip(STATUSES[i["status"]]) + (chip(RISKS[i["risk"]]) if i["risk"] and i["status"] != "done" else "")
        return (
            f'<li><a {opener(i)}><span class="lead">{ui.stream_badge(i["ws"])}'
            f'<span><b>{escape(i["title"])}</b><small>{escape(i["ws"])} · {escape(i["owner"])}</small></span></span>'
            f'<span class="end">{chips}<span class="due">{escape(due_label(i["due"]))}</span>{icon("chevron")}</span></a></li>'
        )

    # --- today -----------------------------------------------------------------------------
    @app.get("/today", response_class=HTMLResponse)
    def today_page(ws: str = "", f: str = "open") -> str:
        ws, f = known(ws), f if f in dict(FILTERS) else "open"
        found = filtered(items(ws), f)
        path = f"/today?f={f}" + (f"&ws={quote(ws)}" if ws else "")
        calls = "".join(
            f'<li><a href="/calendar"><span class="lead"><span><b>{escape(c["subject"] or "Call")}</b>'
            f'<small>{escape(c["label"])} · with {escape(c["sender"])}</small></span></span>'
            f'<span class="end">{icon("chevron")}</span></a></li>' for c in calls_today()
        )
        listing = (
            f'<section class="card"><ul class="rows">{"".join(row(i) for i in found)}</ul></section>' if found else
            '<p class="quiet">No item here. Add a task, or give the agent work on the <a href="/new">Agent work</a> screen.</p>'
        )
        return render("Today", "today", f"""
<p class="crumb">{escape(settings.brand_name)} / Today</p>
<h1>Today</h1>
<p class="lede">The decisions, deadlines and open loops that deserve your attention.</p>
{cap_banner()}
{ws_chips("/today", ws, f=f)}
<div class="toolbar">{tabs("/today", ws, f)}</div>
{new_task_form(path, ws)}
{listing}
{f'<section class="card"><div class="linkrow"><h2>Calls today</h2><a href="/calendar">All calls {icon("arrow")}</a></div><ul class="rows">{calls}</ul></section>' if calls else ""}
{edit_sheets(found, path)}
""", script=sheet_script, dock=dock())

    # --- work ------------------------------------------------------------------------------
    @app.get("/work", response_class=HTMLResponse)
    def work_page(ws: str = "", f: str = "all") -> str:
        ws, f = known(ws), f if f in dict(FILTERS) else "all"
        found = filtered(items(ws), f)
        path = f"/work?f={f}" + (f"&ws={quote(ws)}" if ws else "")

        def card(i: dict) -> str:
            return (
                f'<a class="tile" {opener(i)}><span class="who">{ui.stream_badge(i["ws"])}<span>{escape(i["ws"])}</span></span>'
                f'<b>{escape(i["title"])}</b>' + (f'<span class="what">{escape(i["detail"])}</span>' if i["detail"] else "")
                + f'<span class="chips">{chip(RISKS[i["risk"]]) if i["risk"] else ""}{chip(i["tag"]) if i["tag"] else ""}</span>'
                f'<span class="foot"><span>{escape(i["owner"])}</span><span>{escape(due_label(i["due"]))}</span></span></a>'
            )

        columns = "".join(
            f'<section class="column"><h2>{label} <span>{len(mine)}</span></h2>'
            + ("".join(card(i) for i in mine) or '<p class="quiet">Nothing here yet.</p>') + "</section>"
            for key, (label, _) in STATUSES.items() for mine in [[i for i in found if i["status"] == key]]
        )
        return render("Work", "work", f"""
<p class="crumb">{escape(settings.brand_name)} / Work</p>
<h1>Work</h1>
<p class="lede">One view of everything moving across your workstreams.</p>
{ws_chips("/work", ws, f=f)}
<div class="toolbar">{tabs("/work", ws, f)}
  <span class="actions"><a class="button" href="/studio">{icon("creative")}Studio</a>
    <a class="button" href="/dispatch">{icon("arrow")}Agency dispatch</a>
    <a class="button approve" href="/new">{icon("spark")}Give the agent work</a></span></div>
{new_task_form(path, ws)}
<div class="board">{columns}</div>
{edit_sheets(found, path)}
""", script=sheet_script, dock=dock())

    # --- tasks -----------------------------------------------------------------------------
    def task_fields(form: dict) -> dict:
        text = lambda key: str(form.get(key) or "").strip()  # noqa: E731
        out = {"title": text("title")[:200], "detail": text("detail").replace("\r\n", "\n")[:2000] or None,
               "status": text("status") if text("status") in STATUSES else "todo",
               "risk": text("risk") if text("risk") in RISKS else None,
               "due": text("due")[:16] if DUE.fullmatch(text("due")[:16]) else None}
        if not out["title"]:
            raise HTTPException(400, "a task needs a title")
        return out

    @app.post("/tasks")
    async def create_task(request: Request):
        form = await fields(request)
        ws = known(str(form.get("workstream") or "").strip())
        if not ws:
            raise HTTPException(400, "choose a workstream for the task")
        task_id = db.create_task(workstream=ws, owner=settings.owner_name, **task_fields(form))
        db.audit(None, "task_created", {"task": task_id, "workstream": ws})
        if "text/html" in request.headers.get("accept", ""):
            return back(str(form.get("back") or ""), "/work")
        return {"task": task_id}

    def owned(task_id: str) -> dict:
        task = db.get_task(task_id)
        if not task:
            raise HTTPException(404, "no such task")
        return task

    @app.post("/tasks/{task_id}")
    async def update_task(task_id: str, request: Request):
        owned(task_id)
        form = await fields(request)
        db.update_task(task_id, **task_fields(form))
        return back(str(form.get("back") or ""), "/work")

    @app.post("/tasks/{task_id}/delete")
    async def delete_task(task_id: str, request: Request):
        owned(task_id)
        db.delete_task(task_id)
        db.audit(None, "task_deleted", {"task": task_id})
        return back(str((await fields(request)).get("back") or ""), "/work")

    @app.post("/tasks/{task_id}/decision")
    async def decide_task(task_id: str, request: Request):
        """The owner's call on a task that waits for it, with the reason. It records the
        decision and moves the task; it takes no action outside this system."""
        task = owned(task_id)
        form = await fields(request)
        decision = str(form.get("decision") or "")
        if decision not in DECISIONS:
            raise HTTPException(400, "decision must be approved, changes or declined")
        reason = str(form.get("reason") or "").strip().replace("\r\n", "\n")[:4000] or None
        decision_id = db.record_decision(task["workstream"], task["title"], decision, reason, "task", task_id)
        db.update_task(task_id, decision=decision, status="in_progress" if decision == "changes" else "done")
        db.audit(None, "task_decided", {"task": task_id, "decision": decision})
        return back(f"/queue?done={decision_id}", "/queue")

    def decision_sheets(ws: str | None) -> tuple[list[dict], str]:
        """The owner's tasks that wait for a decision, and the sheet for each (for the Review screen)."""
        waiting = [i for i in items(ws) if i["task"] and i["status"] == "awaiting_approval"]
        sheets = []
        for i in waiting:
            t = i["task"]
            sheets.append(f"""<dialog class="sheet" id="d-t{t}" aria-labelledby="t-d-t{t}" open>
  {sheet_head(f"d-t{t}", i["ws"], chip(STATUSES["awaiting_approval"]) + (chip(RISKS[i["risk"]]) if i["risk"] else ""), i["title"])}
  <div class="sheet-body">
    {f'<p class="context">{icon("info")}<span><strong>Context:</strong> {escape(i["detail"])}</span></p>' if i["detail"] else ""}
    <p class="meta">Accountable owner: {escape(i["owner"])}{f" · due {escape(due_label(i['due']))}" if due_label(i["due"]) else ""}</p>
    <form method="post" action="/tasks/{t}/decision" id="f-t{t}" class="stack">
      <label class="eyebrow" for="c-t{t}">Why this decision?</label>
      <textarea id="c-t{t}" name="reason" rows="3" maxlength="4000"></textarea>
    </form>
    <p class="meta">This records your decision and its reason in Memory. It sends nothing and changes nothing outside this system.</p>
  </div>
  <footer class="sheet-foot">
    <span class="actions">
      <button form="f-t{t}" name="decision" value="changes" class="sendback">{icon("note")}Request changes</button>
      <button form="f-t{t}" name="decision" value="declined" class="reject">Decline</button>
    </span>
    <button form="f-t{t}" name="decision" value="approved" class="approve">{icon("check")}Approve and record</button>
  </footer>
</dialog>""")
        return waiting, "".join(sheets)

    # --- memory ----------------------------------------------------------------------------
    @app.post("/memory")
    async def add_memory(request: Request):
        form = await fields(request)
        ws, kind = known(str(form.get("workstream") or "").strip()), str(form.get("kind") or "")
        text = str(form.get("text") or "").strip().replace("\r\n", "\n")[:4000]
        if not ws or kind not in KNOWLEDGE or not text:
            raise HTTPException(400, "choose a workstream and a kind, and write the note")
        item = db.add_memory(ws, kind, KNOWLEDGE[kind], text)
        db.audit(None, "memory_added", {"item": item, "workstream": ws, "kind": kind})  # it changes what the agent is told
        if "text/html" in request.headers.get("accept", ""):
            return back(f"/library?ws={quote(ws)}#context", "/library")
        return {"item": item}

    @app.post("/memory/{item_id}/delete")
    def delete_memory(item_id: str):
        db.delete_memory(item_id)
        db.audit(None, "memory_removed", {"item": item_id})
        return back("/library#context", "/library")

    @app.post("/think/{run_id}/save")
    def save_draft(run_id: str):
        run = db.get_run(run_id)
        if not run or run["workflow"] != "think" or not (run["outputs"] or {}).get("answer"):
            raise HTTPException(404, "no answer to save")
        if not any(m["kind"] == "draft" and m["title"] == run_id for m in db.list_memory(run["client"])):
            db.add_memory(run["client"], "draft", run_id, f"{(run['inputs'] or {}).get('question', '')}\n\n{run['outputs']['answer']}")
        return back("/library#drafts", "/library")

    def memory_sections(ws: str | None) -> str:
        """Decision library, workstream knowledge and saved drafts, for the Memory screen."""
        mine = lambda rows: [r for r in rows if not ws or r["workstream"] == ws]  # noqa: E731
        made = mine(db.list_decisions())
        library = "".join(
            f"""<li><div class="row-static"><span class="lead">{ui.stream_badge(d["workstream"])}
              <span><b>{escape(d["title"] or "")}</b><small>{escape(d["workstream"])} · {escape((d["created_at"] or "")[:10])}</small>
              {f'<span class="why">{escape(d["reason"])}</span>' if d["reason"] else '<span class="why none">No reason recorded.</span>'}</span></span>
              <span class="end">{chip(DECISIONS.get(d["decision"], (d["decision"], "c-slate")))}</span></div></li>"""
            for d in made[:100]
        )
        notes = [m for m in mine(db.list_memory()) if m["kind"] != "draft"]
        knowledge = "".join(
            f"""<li><div class="row-static"><span class="lead">{ui.stream_badge(m["workstream"])}
              <span><b>{escape(m["text"])}</b><small>{escape(m["workstream"])} · {escape(KNOWLEDGE.get(m["kind"], m["kind"]))}</small></span></span>
              <form method="post" action="/memory/{m["id"]}/delete" class="end"><button class="quiet-button" aria-label="Remove this note">{icon("x")}</button></form></div></li>"""
            for m in notes
        )
        drafts = "".join(
            f'<details><summary>{escape(m["text"].split(chr(10), 1)[0][:140])}</summary><p class="meta">{escape(m["workstream"])} · '
            f'{escape((m["created_at"] or "")[:10])}</p><pre class="say">{escape(m["text"])}</pre>'
            f'<form method="post" action="/memory/{m["id"]}/delete"><button class="reject">Remove this draft</button></form></details>'
            for m in mine(db.list_memory()) if m["kind"] == "draft"
        )
        return f"""<section class="card" id="decisions">
  <div class="linkrow"><h2>Decision library</h2><span class="eyebrow">What and why</span></div>
  {f'<ul class="rows">{library}</ul>' if library else '<p class="quiet">Make a decision in Review and give the reason. It appears here.</p>'}
</section>
<section class="card" id="context">
  <div class="linkrow"><h2>Workstream knowledge</h2><span class="eyebrow">The agent reads this</span></div>
  <p class="quiet">Priorities, brand rules, preferences and lessons for one workstream. The agent takes them as context for
   that workstream's work, and for no other.</p>
  {f'<ul class="rows">{knowledge}</ul>' if knowledge else ""}
  <details class="add"><summary>{icon("plus")}Add to memory</summary>
    <form method="post" action="/memory" class="stack">
      <label>Workstream <select name="workstream" required>{ws_options(ws)}</select></label>
      <label>Kind <select name="kind">{options(KNOWLEDGE, "priority")}</select></label>
      <label>The note <textarea name="text" rows="3" maxlength="4000" required></textarea></label>
      <button class="approve">Add this note</button>
    </form></details>
</section>
<section class="card" id="drafts">
  <div class="linkrow"><h2>Saved drafts</h2></div>
  {drafts or '<p class="quiet">Save an answer from Think to keep it here.</p>'}
</section>"""

    def activity() -> str:
        words = {
            "run_created": "A run started", "task_created": "A task was added", "task_decided": "You decided a task",
            "memory_added": "A note went into memory", "signal_added": "A signal went on the watchlist",
            "skill_change_accepted": "You accepted a change to the rules", "model_route_set": "A model route changed",
            "login_succeeded": "A sign-in", "client_created": "A workstream was created", "client_deleted": "A workstream was deleted",
        }
        rows = "".join(
            f"<tr><td>{escape((a['created_at'] or '')[:16].replace('T', ' '))}</td><td>{escape(words[a['action']])}</td></tr>"
            for a in db.recent_audit(120) if a["action"] in words
        )
        return f"""<section class="card" id="activity"><div class="linkrow"><h2>Activity</h2><span class="eyebrow">UTC</span></div>
  {f'<div class="tablewrap"><table>{rows}</table></div>' if rows else '<p class="quiet">No activity yet.</p>'}</section>"""

    # --- the watchlist ---------------------------------------------------------------------
    @app.post("/signals")
    async def add_signal(request: Request):
        form = await fields(request)
        text = lambda key, limit: str(form.get(key) or "").strip()[:limit]  # noqa: E731
        ws, kind = known(text("workstream", 80)), text("kind", 20)
        if not ws or kind not in SIGNALS or not text("title", 200):
            raise HTTPException(400, "choose a workstream and a kind, and give the signal a title")
        signal = db.add_signal(ws, kind, text("title", 200), text("note", 2000), text("source", 500))
        db.audit(None, "signal_added", {"signal": signal, "workstream": ws, "kind": kind})
        if "text/html" in request.headers.get("accept", ""):
            return back("/intelligence", "/intelligence")
        return {"signal": signal}

    @app.post("/signals/{signal_id}/delete")
    def delete_signal(signal_id: str):
        db.delete_signal(signal_id)
        return back("/intelligence", "/intelligence")

    # --- the feed: sources, and what they bring in ------------------------------------------
    @app.post("/signals/sources")
    async def add_source(request: Request):
        form = await fields(request)
        ws, kind = known(str(form.get("workstream") or "").strip()), str(form.get("kind") or "")
        value = str(form.get("value") or "").strip()[:300]
        if not ws or kind not in ("term", "feed") or not value:
            raise HTTPException(400, "choose a workstream, and give a search term or a feed's address")
        if kind == "feed":
            try:
                signal_scan.check_address(value)
            except signal_scan.FeedError as e:
                raise HTTPException(400, str(e))
        source = db.add_watch_source(ws, kind, value)
        db.audit(None, "watch_source_added", {"source": source, "workstream": ws, "kind": kind})
        if "text/html" in request.headers.get("accept", ""):
            return back("/intelligence#sources", "/intelligence")
        return {"source": source}

    @app.post("/signals/sources/{source_id}/delete")
    def delete_source(source_id: str):
        db.delete_watch_source(source_id)
        db.audit(None, "watch_source_removed", {"source": source_id})
        return back("/intelligence#sources", "/intelligence")

    @app.post("/signals/refresh")
    def refresh_signals(request: Request, background: BackgroundTasks):
        """Read every source now. It runs after this answer; the page shows the result when it is opened again."""
        background.add_task(signal_scan.scan_all, engine)
        if "text/html" in request.headers.get("accept", ""):
            return back("/intelligence#incoming", "/intelligence")
        return {"started": True}

    @app.post("/signals/incoming/{item_id}")
    async def decide_incoming(item_id: str, request: Request):
        """The owner's call on a headline: it joins the watchlist, with its link as the source, or it is dismissed."""
        item = db.get_feed_item(item_id)
        if not item or item["status"] != "new":
            raise HTTPException(404, "no such headline waiting")
        form = await fields(request)
        if form.get("decision") == "add":
            kind = str(form.get("kind") or item["kind"] or "")
            if kind not in SIGNALS:
                raise HTTPException(400, "choose reputation, regulation or opportunity")
            db.add_signal(item["workstream"], kind, item["title"],
                          "A headline from a feed. Read the article before acting on it.",
                          f"{item['publisher'] or 'publisher not given'}, {item['link']}"[:500])
            db.set_feed_item_status(item_id, "added")
        else:
            db.set_feed_item_status(item_id, "dismissed")
        return back("/intelligence#incoming", "/intelligence")

    def incoming_count() -> int:
        return sum(i["kind"] != "none" for i in db.list_feed_items("new"))

    def signal_list(ws: str | None = None, kind: str = "all") -> list[dict]:
        return [s for s in db.list_signals() if (not ws or s["workstream"] == ws) and (kind == "all" or s["kind"] == kind)]

    def watchlist(ws: str | None, kind: str) -> str:
        cards = "".join(
            f"""<section class="card signal">
  <p class="chips">{chip(SIGNALS.get(s["kind"], (s["kind"], "c-slate")))}<span class="tag c-slate">{escape(s["workstream"])}</span></p>
  <h3>{escape(s["title"])}</h3>
  {f'<p>{escape(s["note"])}</p>' if s["note"] else ""}
  <p class="meta">{f"Source: {escape(s['source'])}" if s["source"] else "No source given"} · added {escape((s["created_at"] or "")[:10])}</p>
  <form method="post" action="/signals/{s["id"]}/delete"><button class="quiet-button">Remove</button></form>
</section>""" for s in signal_list(ws, kind)
        )
        sources = [s for s in db.list_watch_sources() if not ws or s["workstream"] == ws]
        incoming = [i for i in db.list_feed_items("new") if (not ws or i["workstream"] == ws)
                    and (kind == "all" or i["kind"] == kind)]
        shown = [i for i in incoming if i["kind"] != "none"]
        set_aside = len(incoming) - len(shown)
        if sources:
            banner = (f'The agent reads your {ui.plural(len(sources), "source")} each hour and lists what is new below. '
                      "A headline is a lead, not a finding: read the article before you act on it. "
                      "Nothing joins your watchlist until you add it.")
        else:
            banner = ("This is your watchlist: each signal is one you add, with its source. Live monitoring is not connected, "
                      "so the agent adds none. Add a source below and the agent reads it each hour.")

        def item_card(i: dict) -> str:
            label = SIGNALS.get(i["kind"], ("Not sorted", "c-slate"))
            when = (i["published_at"] or i["created_at"] or "")[:10]
            pick = options(SIGNALS, i["kind"] if i["kind"] in SIGNALS else "reputation")
            return f"""<section class="card signal">
  <p class="chips">{chip(label)}<span class="tag c-slate">{escape(i["workstream"])}</span></p>
  <h3><a href="{escape(i["link"])}" rel="noopener noreferrer" target="_blank">{escape(i["title"])}</a></h3>
  <p class="meta">{escape(i["publisher"] or "Publisher not given")} · {escape(when)} · a headline from a feed, not checked</p>
  {f'<p class="quiet">The agent\'s reading of the headline: {escape(i["why"])}</p>' if i["why"] else ""}
  <div class="actions">
    <form method="post" action="/signals/incoming/{i["id"]}"><select name="kind" aria-label="Kind">{pick}</select>
      <button name="decision" value="add" class="approve">Add to watchlist</button></form>
    <form method="post" action="/signals/incoming/{i["id"]}"><button name="decision" value="dismiss" class="quiet-button">Dismiss</button></form>
  </div>
</section>"""

        source_rows = "".join(
            f"""<li><div class="row-static"><span class="lead">{ui.stream_badge(s["workstream"])}
              <span><b>{escape(s["value"])}</b><small>{escape(s["workstream"])} · {"a search term, read through Google News" if s["kind"] == "term" else "a feed"}
              {f' · last check failed: {escape(s["last_error"])}' if s["last_error"] else (" · last read " + escape((s["checked_at"] or "")[:16].replace("T", " ")) + " UTC" if s["checked_at"] else " · not read yet")}</small></span></span>
              <form method="post" action="/signals/sources/{s["id"]}/delete" class="end"><button class="quiet-button" aria-label="Remove this source">{icon("x")}</button></form></div></li>"""
            for s in sources
        )
        feed = f"""<section class="card" id="sources">
  <div class="linkrow"><h2>Sources</h2>
    <form method="post" action="/signals/refresh"><button class="quiet-button">Check now</button></form></div>
  <p class="quiet">A search term goes to Google News as a search, so choose terms you are content to send there. A feed is any
   RSS or Atom address that starts with https.</p>
  {f'<ul class="rows">{source_rows}</ul>' if source_rows else ""}
  <details class="add"><summary>{icon("plus")}Add a source</summary>
    <form method="post" action="/signals/sources" class="stack">
      <div class="row two">
        <label>Workstream <select name="workstream" required>{ws_options(ws)}</select></label>
        <label>Kind <select name="kind"><option value="term">A search term</option><option value="feed">A feed's address</option></select></label>
      </div>
      <label>The term, or the address <input type="text" name="value" maxlength="300" required/></label>
      <button class="approve">Watch this</button>
    </form></details>
</section>
<h2 id="incoming">Incoming <span class="meta">{len(shown)} new{f", {set_aside} set aside as not relevant" if set_aside else ""}</span></h2>
<div class="signals">{"".join(item_card(i) for i in shown[:30]) or '<p class="quiet">No new headline from your sources.</p>'}</div>
<h2>Your watchlist</h2>
"""
        return f"""{ws_chips("/intelligence", ws, kind=kind)}
<div class="toolbar">{tabs("/intelligence", ws, kind, (("all", "All"),) + tuple((k, v[0]) for k, v in SIGNALS.items()), "kind")}</div>
<p class="banner" role="status">{banner}</p>
{feed}
<details class="card add"><summary>{icon("plus")}Add a signal</summary>
  <form method="post" action="/signals" class="stack">
    <label>What changed <input type="text" name="title" maxlength="200" required/></label>
    <div class="row">
      <label>Kind <select name="kind">{options(SIGNALS, "reputation")}</select></label>
      <label>Workstream <select name="workstream" required>{ws_options(ws)}</select></label>
    </div>
    <label>Why it matters <span class="or">optional</span><textarea name="note" rows="2" maxlength="2000"></textarea></label>
    <label>Source <span class="or">a link, a publication, a person</span><input type="text" name="source" maxlength="500"/></label>
    <button class="approve">Add this signal</button>
  </form></details>
<div class="signals">{cards or '<p class="quiet">No signal on the watchlist yet.</p>'}</div>"""

    # --- core ------------------------------------------------------------------------------
    @app.get("/core", response_class=HTMLResponse)
    def core_page() -> str:
        core = settings.core
        dimensions = "".join(
            f'<a class="tile" href="/think?start={quote("Think with me about " + d["name"] + ": ")}"><b>{escape(d["name"])}</b>'
            f'<span class="what">{escape(d["line"])}</span><span class="foot"><span>Explore the thinking</span>{icon("arrow")}</span></a>'
            for d in core.dimensions
        )
        principles = "".join(
            f'<li><span class="num c-purple">{n:02d}</span><b>{escape(p)}</b></li>' for n, p in enumerate(core.principles, 1)
        )
        return render("Chidimma Core", "core", f"""
<p class="crumb">{escape(settings.brand_name)} / Core</p>
<h1>Your judgement, at the centre.</h1>
<p class="lede">One {escape(settings.owner_name)}. A consistent standard across every workstream.</p>
<h2>One core intelligence. Many dimensions.</h2>
<p class="lede">Your principles stay consistent. The context changes with the work.</p>
<div class="board six">{dimensions or '<p class="quiet">No dimension is set in config/core.yaml.</p>'}</div>
<section class="card">
  <div class="linkrow"><h2>The principles that hold it together</h2><span class="eyebrow">Sent with every step of the agent's work</span></div>
  {f'<ol class="principles">{principles}</ol>' if principles else '<p class="quiet">No principle is set in config/core.yaml.</p>'}
</section>
""", dock=dock())

    return SimpleNamespace(
        items=items, filtered=filtered, open_count=open_count, ws_chips=ws_chips, known=known, chip=chip,
        decision_sheets=decision_sheets, memory_sections=memory_sections, activity=activity,
        watchlist=watchlist, signal_list=signal_list, due_label=due_label, row=row, incoming_count=incoming_count,
    )
