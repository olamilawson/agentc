"""Agency dispatch: an approved brief goes to an agency, with a window to recall it.

    prepare (code) -> owner approval (interrupt) -> queue (code) -> done
                                                       |
                       held until the window ends, then released by release_due()

There is no model step: the owner writes the brief and chooses the files, and
the mail carries exactly what the owner approved.

A mail cannot be unsent, so the recall is real only before sending. The
approved brief is held for the window (15 minutes unless the owner sets
another). In that time the owner can recall it and nothing leaves. Once it has
gone, recalling it means sending the agency a halt notice, which is itself a
release and waits for the owner's approval like any other.

The send goes through the tool gateway as a release: it needs the owner's
approval of this exact content, and the same approved brief is sent once.
"""

from __future__ import annotations

import re
import sys
from datetime import datetime, timedelta, timezone
from typing import TypedDict

from langgraph.graph import END, START, StateGraph

from personal_agent.clients import ClientScope
from personal_agent.engine.approval import make_approval_node
from personal_agent.gateway.tools import UncertainOutcome
from personal_agent.harness.contract import StepFailed

WORKFLOW = "agency-dispatch"
TOOL = "send_agency_brief"
ADDRESS = re.compile(r"[^@\s<>,;]+@[^@\s<>,;]+\.[^@\s<>,;]+")
# Microsoft Graph takes a file of up to 3 MB as a plain attachment.
MAX_FILE_BYTES, MAX_TOTAL_BYTES, MAX_HOLD_MINUTES = 3_000_000, 10_000_000, 120


class DispatchState(TypedDict, total=False):
    run_id: str
    client: str
    inputs: dict
    flags: list[str]
    payload: dict
    approval_error: str | None
    approved_hash: str
    dispatch_id: str
    error: str | None


def halt_text(original: dict, when: str) -> tuple[str, str]:
    """The notice that asks an agency to stop work on a brief already sent."""
    return (
        f"Please halt: {original['subject']}",
        f"Please stop work on the brief \"{original['subject']}\", sent to you on {when}.\n\n"
        "It has been recalled. Do not book media, start production or share it further. "
        "A revised brief, or word that this one stands, will follow.\n\n"
        "Please confirm that you have this notice.",
    )


def dispatch_outputs(state: dict) -> dict | None:
    if not state.get("payload"):
        return None
    return {"dispatch_id": state.get("dispatch_id"), "to": state["payload"]["to"], "subject": state["payload"]["subject"]}


def dispatch_rejected(db, run: dict, comment: str | None) -> None:
    """The owner rejected a halt notice: the brief it would have halted stands as sent."""
    halts = (run["inputs"] or {}).get("halts")
    if halts and db.claim_dispatch(halts, "halt_pending", "sent"):
        db.audit(run["id"], "halt_notice_rejected", {"dispatch": halts})


def build_dispatch_graph(h):
    db = h.db

    def prepare(state: DispatchState) -> dict:
        inputs = state["inputs"] or {}
        to = str(inputs.get("to") or "").strip()
        if not ADDRESS.fullmatch(to):
            raise StepFailed(f"'{to}' is not an email address, so the brief was not prepared")
        files = {f["path"]: f["size"] for f in h.clients.list_files(ClientScope(state["client"]))}
        attachments = [str(p) for p in inputs.get("attachments") or []]
        for path in attachments:
            if path not in files:
                raise StepFailed(f"'{path}' is not in the {state['client']} folder, so the brief was not prepared")
            if files[path] > MAX_FILE_BYTES:
                raise StepFailed(f"'{path}' is over 3 MB; put a link to it in the brief instead of attaching it")
        if sum(files[p] for p in attachments) > MAX_TOTAL_BYTES:
            raise StepFailed("the attachments come to more than 10 MB together; send fewer, or link to them")
        return {"payload": {
            "client": state["client"], "agency": str(inputs.get("agency") or "").strip() or to, "to": to,
            "subject": str(inputs.get("subject") or "").strip(), "body": str(inputs.get("body") or ""),
            "attachments": attachments,
        }}

    def about(state: DispatchState) -> dict:
        inputs, payload = state["inputs"] or {}, state["payload"]
        hold = _hold_minutes(inputs)
        notes = [f"To {payload['agency']} at {payload['to']}."]
        notes.append("Attached: " + ", ".join(payload["attachments"]) + "." if payload["attachments"] else "No file is attached.")
        if inputs.get("kind") == "halt":
            notes.append("This asks the agency to stop. It goes at once: a halt has no recall window.")
        else:
            notes.append(f"It is held for {hold} minutes after you approve, and you can recall it in that time."
                         if hold else "It goes at once: you set no recall window.")
        return {"action": "halt" if inputs.get("kind") == "halt" else "dispatch", "notes": notes}

    approval = make_approval_node(db, lambda state: state["payload"], about)

    def queue(state: DispatchState) -> dict:
        inputs, payload = state["inputs"] or {}, state["payload"]
        halt = inputs.get("kind") == "halt"
        release_at = datetime.now(timezone.utc) + timedelta(minutes=0 if halt else _hold_minutes(inputs))
        dispatch_id = db.create_dispatch(
            run_id=state["run_id"], workstream=state["client"], kind="halt" if halt else "brief",
            agency=payload["agency"], recipient=payload["to"], subject=payload["subject"], body=payload["body"],
            attachments=payload["attachments"], directive=inputs.get("directive") or None,
            revision_of=inputs.get("revision_of") or None, halts=inputs.get("halts") or None,
            status="held", release_at=release_at.isoformat(timespec="seconds"), approved_hash=state.get("approved_hash"),
        )
        db.audit(state["run_id"], "dispatch_queued", {"dispatch": dispatch_id, "release_at": release_at.isoformat(timespec="seconds")})
        return {"dispatch_id": dispatch_id, "error": None}

    builder = StateGraph(DispatchState)
    builder.add_node("prepare", prepare)
    builder.add_node("approval", approval)
    builder.add_node("queue", queue)
    builder.add_edge(START, "prepare")
    builder.add_edge("prepare", "approval")
    builder.add_conditional_edges("approval", lambda s: "approval" if s.get("approval_error") else "queue",
                                  {"approval": "approval", "queue": "queue"})
    builder.add_edge("queue", END)
    return builder


def _hold_minutes(inputs: dict) -> int:
    try:
        return max(0, min(MAX_HOLD_MINUTES, int(inputs.get("hold_minutes", 15))))
    except (TypeError, ValueError):
        return 15


def payload_of(dispatch: dict) -> dict:
    """The exact content the owner approved, rebuilt from the dispatch record."""
    return {
        "client": dispatch["workstream"], "agency": dispatch["agency"], "to": dispatch["recipient"],
        "subject": dispatch["subject"], "body": dispatch["body"], "attachments": dispatch["attachments"] or [],
    }


def release_due(engine, now: datetime | None = None) -> list[dict]:
    """Send every held dispatch whose window has ended. Safe to call from several
    places at once: each dispatch is claimed before it is sent, and the gateway
    sends the same approved content once."""
    db, moment = engine.db, (now or datetime.now(timezone.utc)).isoformat(timespec="seconds")
    done = []
    for d in db.list_dispatches(status="held"):
        if d["release_at"] > moment or not db.claim_dispatch(d["id"], "held", "sending"):
            continue
        stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
        try:
            result = engine.harness.tools.call(d["run_id"], "dispatch", [TOOL], TOOL, payload_of(d))
        except UncertainOutcome as e:
            # Never retried: it may have gone. The owner checks Sent Items.
            db.update_dispatch(d["id"], status="uncertain", note=str(e))
        except Exception as e:  # a definite failure: nothing was sent
            db.update_dispatch(d["id"], status="failed", note=f"{type(e).__name__}: {e}"[:500])
        else:
            if result.get("sent"):
                db.update_dispatch(d["id"], status="sent", sent_at=stamp, note=None)
                if d["kind"] == "halt" and d["halts"]:
                    db.update_dispatch(d["halts"], status="halted", recalled_at=stamp)
            else:
                db.update_dispatch(d["id"], status="outbox", note=result.get("note"))
        db.audit(d["run_id"], "dispatch_released", {"dispatch": d["id"], "status": db.get_dispatch(d["id"])["status"]})
        done.append(db.get_dispatch(d["id"]))
    return done


def recall(engine, dispatch_id: str) -> dict:
    """Recall a dispatch. Held: it is stopped and nothing leaves. Already sent: a halt
    notice is prepared for the owner's approval; returns {"halt_run": id} for it."""
    db = engine.db
    d = db.get_dispatch(dispatch_id)
    if not d:
        raise KeyError(dispatch_id)
    if db.claim_dispatch(dispatch_id, "held", "recalled"):
        db.update_dispatch(dispatch_id, recalled_at=datetime.now(timezone.utc).isoformat(timespec="seconds"))
        db.audit(d["run_id"], "dispatch_recalled", {"dispatch": dispatch_id, "before_sending": True})
        return {"recalled": True}
    d = db.get_dispatch(dispatch_id)
    if d["status"] != "sent" or d["kind"] != "brief":
        raise ValueError(f"this dispatch is {d['status'].replace('_', ' ')}, so there is nothing to recall")
    subject, body = halt_text(d, (d["sent_at"] or "")[:16].replace("T", " at ") + " UTC")
    run_id = engine.start_run(WORKFLOW, d["workstream"], {
        "kind": "halt", "halts": dispatch_id, "agency": d["agency"], "to": d["recipient"], "subject": subject,
        "body": body, "attachments": [], "hold_minutes": 0,
    })
    db.update_dispatch(dispatch_id, status="halt_pending")
    db.audit(d["run_id"], "dispatch_recalled", {"dispatch": dispatch_id, "before_sending": False, "halt_run": run_id})
    return {"halt_run": run_id}


def main(argv: list[str] | None = None) -> int:
    """`python -m personal_agent.engine.dispatch`: send what is due. The server's cron runs it each minute."""
    from personal_agent.config import load_settings
    from personal_agent.db import Database
    from personal_agent.engine.runner import Engine

    settings = load_settings()
    released = release_due(Engine(settings, Database(settings.database_url)))
    if not released:
        print("no dispatch is due")
    for d in released:
        print(f"{d['subject']} to {d['recipient']}: {d['status']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
