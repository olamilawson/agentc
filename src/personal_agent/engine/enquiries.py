"""Requests from outside, and the invite links that let a person send a brief.

The status of an enquiry is a state machine, and this module is the one place
that moves it:

    public page --receive()--> received --invite()--> invited --last brief in--> briefed
                                  |                     |  \\--expires_at passes, settle()--> expired
                                  +--decline()--> declined  \\--cancel()-------------------> cancelled
    owner --invite_direct()-----------------------> invited

Nothing in a request reaches a model, so a request costs nothing. Only a brief
sent through a live link starts an evaluation, and at most
evaluations_per_month of those start by themselves each month. Past that, or
past the monthly spending cap, the brief is kept and waits for the owner.

Mailing a link to the person is a release like any send: the "enquiry-invite"
run waits for the owner's approval of the exact mail. The owner can also copy
the link and send it herself.
"""

from __future__ import annotations

import hashlib
import hmac
import re
import secrets
from datetime import datetime, timedelta, timezone
from typing import TypedDict

from langgraph.graph import END, START, StateGraph

from personal_agent import costs
from personal_agent.clients import ClientScope, parse_document
from personal_agent.engine.approval import make_approval_node
from personal_agent.gateway.tools import ApprovalLapsed, ApprovalRequired, UncertainOutcome

WORKFLOW = "enquiry-invite"
STATES = {
    "received": ("Waiting for your decision", "c-amber"), "invited": ("Link live", "c-emerald"),
    "briefed": ("Brief received", "c-blue"), "declined": ("Declined", "c-slate"),
    "cancelled": ("Link cancelled", "c-rose"), "expired": ("Link expired", "c-slate"),
}
# Settled: nothing more can happen to these, and after keep_days they are deleted.
SETTLED = ("declined", "cancelled", "expired")
ADDRESS = re.compile(r"[^@\s<>,;]+@[^@\s<>,;]+\.[^@\s<>,;]+")
PHONE = re.compile(r"\+?[0-9 ()\-]{7,24}")
LIMITS = {"first_name": 80, "last_name": 80, "organisation": 120, "role": 120, "email": 254, "phone": 24, "message": 1500}
# A brief's files: what the agent can read, and what it keeps for the owner without reading.
READABLE = (".pdf", ".docx", ".txt", ".md")
KEPT = (".pptx", ".png", ".jpg", ".jpeg", ".webp")
MAX_FILES, MAX_FILE_BYTES, MAX_TOTAL_BYTES = 5, 10_000_000, 24_000_000
MAX_BRIEF_CHARS = 60_000


class Refused(Exception):
    """A request or a brief that was not taken, with the reason in words for the person."""


def stamp(moment: datetime | None = None) -> str:
    return (moment or datetime.now(timezone.utc)).isoformat(timespec="seconds")


def address_key(address: str, secret: str) -> str:
    """The network address, hashed with the site's secret: enough to count requests, not to know who sent them."""
    return hmac.new(secret.encode(), address.encode(), hashlib.sha256).hexdigest()[:32]


def check_person(fields: dict) -> dict[str, str]:
    """The fields the person must give, each checked. Returns {field: what is wrong}."""
    wrong = {}
    for name, label in (("first_name", "your first name"), ("last_name", "your last name"),
                        ("organisation", "where you work, or what you do"), ("role", "your role")):
        if not fields.get(name):
            wrong[name] = f"Add {label}."
    if not ADDRESS.fullmatch(fields.get("email") or ""):
        wrong["email"] = "Add an email address, such as name@company.com."
    if not PHONE.fullmatch(fields.get("phone") or "") or sum(c.isdigit() for c in fields.get("phone") or "") < 7:
        wrong["phone"] = "Add a phone number with its country code, such as +234 803 000 0000."
    return wrong


def clean(form) -> dict:
    """The person's fields from a form, trimmed to their limits."""
    return {k: str(form.get(k) or "").strip()[:n].replace("\r\n", "\n") for k, n in LIMITS.items()}


# --- transitions --------------------------------------------------------------------------------

def receive(db, policy, fields: dict, topic: str, key: str, marketing: bool) -> str:
    """A request from the public page. Calls no model. Raises Refused past the limits."""
    since = stamp(datetime.now(timezone.utc) - timedelta(hours=24))
    if db.count_enquiries(address_key=key, since=since) >= policy.per_address_per_day:
        raise Refused("This connection has sent several requests today. Try again tomorrow.")
    if db.count_enquiries(status="received") >= policy.waiting_cap:
        raise Refused("Requests are closed for now while I catch up. Try again in a few days.")
    now = stamp()
    enquiry_id = db.create_enquiry(
        status="received", origin="request", topic=topic or None, address_key=key, terms_at=now,
        marketing_at=now if marketing else None, briefs=[], **fields,
    )
    db.audit(None, "enquiry_received", {"enquiry": enquiry_id})
    return enquiry_id


def _link_fields(policy, workstream: str, days: int | None, briefs: int | None) -> dict:
    days = max(1, min(30, days or policy.invite_days))
    return {
        "workstream": workstream, "token": secrets.token_urlsafe(24),
        "expires_at": stamp(datetime.now(timezone.utc) + timedelta(days=days)),
        "briefs_left": max(1, min(5, briefs or policy.briefs_per_invite)), "decided_at": stamp(),
    }


def invite(db, policy, enquiry_id: str, workstream: str, days: int | None = None, briefs: int | None = None) -> None:
    if not db.claim_enquiry(enquiry_id, "received", "invited", **_link_fields(policy, workstream, days, briefs)):
        raise Refused("This request has already been decided.")
    db.audit(None, "enquiry_invited", {"enquiry": enquiry_id, "workstream": workstream})


def invite_direct(db, policy, fields: dict, workstream: str, days: int | None = None, briefs: int | None = None) -> str:
    """An invite the owner makes for someone she already knows. The person agrees to the terms on the brief form."""
    enquiry_id = db.create_enquiry(status="invited", origin="owner", briefs=[], **fields,
                                   **_link_fields(policy, workstream, days, briefs))
    db.audit(None, "enquiry_invited", {"enquiry": enquiry_id, "workstream": workstream, "direct": True})
    return enquiry_id


def decline(db, enquiry_id: str, note: str = "") -> None:
    if not db.claim_enquiry(enquiry_id, "received", "declined", decided_at=stamp(), note=note or None):
        raise Refused("This request has already been decided.")
    db.audit(None, "enquiry_declined", {"enquiry": enquiry_id})


def cancel(db, enquiry_id: str) -> None:
    if not db.claim_enquiry(enquiry_id, "invited", "cancelled", decided_at=stamp()):
        raise Refused("This link is no longer live, so there is nothing to cancel.")
    db.audit(None, "enquiry_link_cancelled", {"enquiry": enquiry_id})


def settle(db, now: datetime | None = None) -> None:
    """Links whose time has passed become expired. Called before enquiries are shown or a link is opened."""
    moment = stamp(now)
    for e in db.list_enquiries(status="invited"):
        if e["expires_at"] and e["expires_at"] <= moment and db.claim_enquiry(e["id"], "invited", "expired"):
            db.audit(None, "enquiry_link_expired", {"enquiry": e["id"]})


def link_state(db, token: str) -> tuple[str, dict | None]:
    """What a link opens, for the person holding it: live | briefed | expired | cancelled | unknown."""
    settle(db)
    e = db.enquiry_by_token(token)
    if e is None or e["status"] not in ("invited", "briefed", "expired", "cancelled"):
        return "unknown", None
    return ("live" if e["status"] == "invited" else e["status"]), e


def forget_settled(db, policy, now: datetime | None = None) -> int:
    """Delete declined, cancelled and expired requests older than keep_days. The retention job calls it."""
    settle(db, now)
    cutoff = stamp((now or datetime.now(timezone.utc)) - timedelta(days=policy.keep_days))
    gone = db.delete_enquiries(SETTLED, cutoff)
    if gone:
        db.audit(None, "enquiries_forgotten", {"count": gone, "older_than_days": policy.keep_days})
    return gone


# --- a brief through a link ----------------------------------------------------------------------

BRIEF_FIELDS = {"project": 160, "objective": 3000, "audience": 2000, "deliverables": 2000, "budget": 200,
                "dates": 300, "notes": 4000}


def check_brief(brief: dict) -> dict[str, str]:
    wrong = {}
    if not brief.get("project"):
        wrong["project"] = "Give the project a name."
    if len(brief.get("objective") or "") < 20:
        wrong["objective"] = "Say what the work should achieve, in a sentence or two."
    return wrong


def check_files(files: list[tuple[str, bytes]]) -> str | None:
    if len(files) > MAX_FILES:
        return f"Attach up to {MAX_FILES} files."
    for name, data in files:
        if not name.lower().endswith(READABLE + KEPT):
            return f"{name} is a kind of file this form does not take. Send PDF, Word, PowerPoint, text or image files."
        if len(data) > MAX_FILE_BYTES:
            return f"{name} is larger than 10 MB. Send a smaller copy, or put a link to it in the notes."
    if sum(len(d) for _, d in files) > MAX_TOTAL_BYTES:
        return "The files come to more than 24 MB together. Send fewer, or link to the large ones."
    return None


def evaluations_this_month(db) -> int:
    _, since, _ = costs.month_bounds()
    return sum(1 for e in db.list_enquiries() for b in e["briefs"] or []
               if b.get("run_id") and not b.get("by_hand") and b["at"] >= since)


def compose(e: dict, brief: dict, files: list[str], texts: list[tuple[str, str]]) -> str:
    """The brief as the agent reads it: what the person typed, then the text of their files."""
    who = f"{e['first_name']} {e['last_name']}, {e['role']} at {e['organisation']} ({e['email']})"
    lines = [f"Brief sent through a private link by {who}.", ""]
    for key, label in (("project", "Project"), ("objective", "What the work should achieve"), ("audience", "Audience"),
                       ("deliverables", "Deliverables"), ("budget", "Budget"), ("dates", "Key dates"), ("notes", "Notes")):
        if brief.get(key):
            lines += [f"{label}: {brief[key]}", ""]
    if files:
        lines += ["Attached files: " + ", ".join(files), ""]
    for name, text in texts:
        lines += [f"--- {name} ---", text.strip(), ""]
    return "\n".join(lines)[:MAX_BRIEF_CHARS]


def take_brief(engine, e: dict, person: dict, brief: dict, files: list[tuple[str, bytes]], marketing: bool) -> dict:
    """A brief through a live link: saved to the workstream's folder, then evaluated if this month allows.
    Returns the brief's record. Raises Refused if the link is no longer live."""
    db, policy, clients = engine.db, engine.settings.enquiries, engine.harness.clients
    if not db.take_brief_slot(e["id"], stamp()):
        raise Refused("This link is no longer live, so it took no brief.")
    scope = clients.ensure(e["workstream"])
    saved, texts = [], []
    for name, data in files:
        path = clients.store_file(scope, name, data)
        saved.append(path)
        if name.lower().endswith(READABLE):
            try:
                texts.append((path, parse_document(data, name)["text"]))
            except Exception:  # a file that cannot be read is kept, and the agent is told
                texts.append((path, "(this file could not be read)"))
    day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    text = compose({**e, **person}, brief, saved, texts)
    record_path = clients.store_file(scope, f"brief-{day}-{person['first_name'] or 'client'}.md", text.encode("utf-8"))
    entry = {"at": stamp(), "file": record_path, "files": saved, "project": brief.get("project"),
             "run_id": None, "by_hand": False}
    now = stamp()
    updates = {k: v for k, v in person.items() if v}
    if not e.get("terms_at"):
        updates["terms_at"] = now
    if marketing and not e.get("marketing_at"):
        updates["marketing_at"] = now
    if evaluations_this_month(db) < policy.evaluations_per_month:
        try:
            entry["run_id"] = engine.start_run("brief-evaluation", scope.name, {"brief_text": text, "origin": "invite"},
                                               by_hand=False, work=False)
        except costs.MonthlyCapReached:
            pass  # kept; the owner starts it by hand
    briefs = (db.get_enquiry(e["id"])["briefs"] or []) + [entry]
    db.update_enquiry(e["id"], briefs=briefs, **updates)
    if db.get_enquiry(e["id"])["briefs_left"] <= 0:
        db.claim_enquiry(e["id"], "invited", "briefed")
    db.audit(entry["run_id"], "enquiry_brief_received", {"enquiry": e["id"], "evaluating": bool(entry["run_id"])})
    return entry


def evaluate_held(engine, enquiry_id: str, index: int) -> str:
    """The owner starts the evaluation of a brief that was kept waiting. Returns the run id."""
    db = engine.db
    e = db.get_enquiry(enquiry_id)
    briefs = list(e["briefs"] or []) if e else []
    if not (0 <= index < len(briefs)) or briefs[index].get("run_id"):
        raise Refused("That brief is already being evaluated.")
    scope = ClientScope(e["workstream"])
    text = engine.harness.clients.read_bytes(scope, briefs[index]["file"]).decode("utf-8", errors="replace")
    run_id = engine.start_run("brief-evaluation", scope.name, {"brief_text": text, "origin": "invite"}, work=False)
    briefs[index] = {**briefs[index], "run_id": run_id, "by_hand": True}
    db.update_enquiry(enquiry_id, briefs=briefs)
    return run_id


# --- mailing the link: a release that waits for the owner -----------------------------------------

def invite_mail(e: dict, link: str, policy, owner: str) -> tuple[str, str]:
    expires = datetime.fromisoformat(e["expires_at"])
    count = e["briefs_left"]
    return (
        f"Your private link to share a brief with {policy.name}",
        f"Hello {e['first_name']},\n\n"
        f"Thank you for your interest in working with me. This private link lets you share your brief with "
        f"{policy.name}, the AI agent that works alongside me:\n\n{link}\n\n"
        f"The link works until {expires.day} {expires:%B %Y} and accepts {count} brief{'s' if count != 1 else ''}. "
        f"Please keep it to yourself, since anyone holding it can use it.\n\n"
        f"{policy.name} reads your brief first and prepares notes for me. I review everything before I reply.\n\n"
        f"{owner}",
    )


class InviteState(TypedDict, total=False):
    run_id: str
    client: str
    inputs: dict
    approval_error: str | None
    approved_hash: str
    result: dict
    error: str | None


def payload_of(state: dict) -> dict:
    i = state["inputs"]
    return {"client": state["client"], "to": i["to"], "subject": i["subject"], "body": i["body"]}


def about(state: dict) -> dict:
    return {"action": "send", "notes": [f"This mails the invite link to {state['inputs']['to']}.",
                                        "The link works whether or not you send this mail."]}


def invite_outputs(state: dict) -> dict | None:
    if not state.get("result"):
        return None
    return {"enquiry": state["inputs"]["enquiry"], "to": state["inputs"]["to"], **state["result"]}


def build_invite_graph(h):
    approval = make_approval_node(h.db, payload_of, about)

    def send(state: InviteState) -> dict:
        try:
            result = h.tools.call(state["run_id"], "send", ["send_mail"], "send_mail", payload_of(state))
        except (ApprovalRequired, ApprovalLapsed) as e:
            return {"approval_error": str(e)}
        except UncertainOutcome as e:  # never retried: it may have gone
            return {"result": {"sent": None, "note": str(e)}, "approval_error": None}
        return {"result": {"sent": bool(result.get("sent")), "note": result.get("note")}, "approval_error": None, "error": None}

    builder = StateGraph(InviteState)
    builder.add_node("approval", approval)
    builder.add_node("send", send)
    builder.add_edge(START, "approval")
    builder.add_conditional_edges("approval", lambda s: "approval" if s.get("approval_error") else "send",
                                  {"approval": "approval", "send": "send"})
    builder.add_conditional_edges("send", lambda s: "approval" if s.get("approval_error") else END,
                                  {"approval": "approval", END: END})
    return builder


def mail_link(engine, enquiry_id: str, link: str) -> str:
    """Prepare the mail that carries a live link, for the owner's approval in Review. Returns the run id."""
    db = engine.db
    e = db.get_enquiry(enquiry_id)
    if not e or e["status"] != "invited":
        raise Refused("This link is no longer live, so there is nothing to mail.")
    if e["email_run"]:
        run = db.get_run(e["email_run"])
        if run and run["status"] not in ("failed", "cancelled"):
            raise Refused("The mail for this link has already been prepared.")
    subject, body = invite_mail(e, link, engine.settings.enquiries, engine.settings.owner_name)
    run_id = engine.start_run(WORKFLOW, e["workstream"], {
        "enquiry": enquiry_id, "to": e["email"], "subject": subject, "body": body,
    })
    db.update_enquiry(enquiry_id, email_run=run_id)
    return run_id
