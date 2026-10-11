"""Engine: creates runs, drives the graph, records approvals, syncs the run record.

Run statuses (PRD): queued, running, waiting_for_approval, waiting_for_input,
done, failed, cancelled. A run that is waiting persists across restarts (its
LangGraph checkpoint is durable) and resumes from its last checkpoint.
"""

from __future__ import annotations

import sqlite3
import time
from datetime import datetime
from dataclasses import dataclass, field
from typing import Callable

from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.types import Command
from sqlalchemy.engine import make_url

from personal_agent import costs, tracing
from personal_agent.clients import ClientScope, ClientStore, parse_document
from personal_agent.config import Settings
from personal_agent.db import Database, new_id
from personal_agent.engine.brief_evaluation import brief_contracts, brief_outputs, build_brief_graph
from personal_agent.engine.call_change import build_call_change_graph, call_change_contracts, call_change_outputs
from personal_agent.engine.creative_review import build_creative_graph, creative_contracts, creative_outputs
from personal_agent.engine.graph import ROLE, DraftIn, DraftOut, build_test_graph
from personal_agent.engine.sponsorship_triage import (
    build_sponsorship_graph,
    on_rejected as sponsorship_rejected,
    sponsorship_contracts,
    sponsorship_outputs,
)
from personal_agent.engine.strategy_drafting import build_drafting_graph, drafting_contracts, drafting_outputs
from personal_agent.graph_mail import GraphError, GraphMail, from_settings as graph_from_settings
from personal_agent.gateway.tools import ToolClass, ToolGateway, ToolSpec, content_hash
from personal_agent.harness.budgets import BudgetExceeded, BudgetTracker
from personal_agent.harness.contract import StepContract, StepFailed
from personal_agent.harness.models import ModelGateway
from personal_agent.harness.runner import NodeRunner
from personal_agent.rendering import Page, render_file
from personal_agent.skills.loader import load_library

PENDING_STATUSES = {"waiting_for_approval", "waiting_for_input"}


@dataclass
class Harness:
    settings: Settings
    db: Database
    gateway: ModelGateway
    contracts: dict[str, StepContract]
    skill_text: str
    skills: dict[str, str] = field(default_factory=dict)
    mail: GraphMail | None = None  # the Microsoft Graph mailbox, when connected
    tools: ToolGateway = field(init=False)
    clients: ClientStore = field(init=False)

    def __post_init__(self):
        self.tools = ToolGateway(self.db)
        self.clients = ClientStore(self.settings.clients_dir, self.db)
        # Foundation stub for a release tool; the real Microsoft Graph send tool
        # joins with the same class and approval semantics in the sponsorship phase.
        self.tools.register(
            ToolSpec(
                name="send_test_message",
                cls=ToolClass.RELEASE,
                fn=lambda payload: {"sent": True, "subject": payload["subject"]},
            )
        )
        # Read-class client tools. Every payload names its client; the gateway
        # refuses any payload whose client differs from the run's scope.
        self.tools.register(
            ToolSpec(
                name="read_client_file",
                cls=ToolClass.READ,
                fn=lambda payload: self._read_client_file(payload),
            )
        )
        self.tools.register(
            ToolSpec(
                name="search_client_files",
                cls=ToolClass.READ,
                fn=lambda payload: {
                    "client": payload["client"],
                    "hits": self.clients.search(ClientScope(payload["client"]), payload["query"]),
                },
            )
        )
        self.tools.register(
            ToolSpec(name="render_client_file", cls=ToolClass.READ, fn=self._render_client_file)
        )
        self.tools.register(
            ToolSpec(
                name="list_client_files",
                cls=ToolClass.READ,
                fn=lambda payload: {
                    "client": payload["client"],
                    "files": self.clients.list_files(ClientScope(payload["client"])),
                },
            )
        )
        # Release-class mail. With Microsoft Graph connected the approved reply is
        # sent through the mailbox; without it the reply is held in the local
        # outbox and reported as not sent. Same name, class and approval either way.
        self.tools.register(ToolSpec(name="send_mail", cls=ToolClass.RELEASE, fn=self._send_mail))
        # Read-class: earlier mail to or from an address in the connected mailbox.
        self.tools.register(ToolSpec(name="search_mail", cls=ToolClass.READ, fn=self._search_mail))
        # Release-class: an event with an attendee. Creating it sends the invitation,
        # so it needs an approval matching the exact event, like any send.
        self.tools.register(ToolSpec(name="create_calendar_event", cls=ToolClass.RELEASE, fn=self._create_event))
        self.tools.register(ToolSpec(name="change_calendar_event", cls=ToolClass.RELEASE, fn=self._change_event))
        self.tools.register(ToolSpec(name="cancel_calendar_event", cls=ToolClass.RELEASE, fn=self._cancel_event))
        # Read-class: when the owner's calendar is busy. Free and busy times only.
        self.tools.register(ToolSpec(name="read_calendar", cls=ToolClass.READ, fn=self._read_calendar))
        # Draft-class: a document in the run's own client folder (reversible).
        self.tools.register(
            ToolSpec(
                name="write_client_file",
                cls=ToolClass.DRAFT,
                fn=lambda payload: {
                    "client": payload["client"],
                    "file": self.clients.store_file(
                        ClientScope(payload["client"]), payload["filename"], payload["text"].encode("utf-8")
                    ),
                },
            )
        )

    def render_work(self, client: str, paths: list[str]) -> list[Page]:
        """Creative work in one client's folder as pages, numbered across the files."""
        scope, pages = ClientScope(client), []
        for path in paths:
            pages += render_file(
                self.clients.read_bytes(scope, path), path,
                self.settings.creative_review.image_max_edge, first_number=len(pages) + 1,
            )
        return pages

    def _render_client_file(self, payload: dict) -> dict:
        # Read-class: page text and whether each page has an image. The image
        # bytes are never returned here, so they stay out of the tool-call log.
        data = self.clients.read_bytes(ClientScope(payload["client"]), payload["path"])
        pages = render_file(
            data, payload["path"], self.settings.creative_review.image_max_edge,
            first_number=payload.get("first_number", 1),
        )
        return {
            "client": payload["client"],
            "path": payload["path"],
            "pages": [
                {"source": p.source, "number": p.number, "text": p.text[:4000], "has_image": p.image is not None}
                for p in pages
            ],
        }

    def _send_mail(self, payload: dict) -> dict:
        if self.mail is None:
            return self._hold_mail(payload)
        return self.mail.send_reply(
            payload["to"], payload["subject"], payload["body"], payload.get("in_reply_to")
        )

    def _search_mail(self, payload: dict) -> dict:
        if self.mail is None:
            return {"connected": False, "messages": []}
        return {"connected": True, "messages": self.mail.search_correspondence(payload["address"])}

    def _create_event(self, payload: dict) -> dict:
        if self.mail is None:
            raise GraphError("there is no calendar connection")
        return self.mail.create_event(
            payload["subject"], datetime.fromisoformat(payload["start"]), datetime.fromisoformat(payload["end"]),
            payload["to"], payload["note"],
            # The same approved event always carries the same id, so Microsoft creates it once.
            transaction_id=content_hash(payload)[:36],
            online=self.settings.calendar.online_meeting,
        )

    def _change_event(self, payload: dict) -> dict:
        if self.mail is None:
            raise GraphError("there is no calendar connection")
        return self.mail.change_event(
            payload["event_id"], datetime.fromisoformat(payload["start"]), datetime.fromisoformat(payload["end"])
        )

    def _cancel_event(self, payload: dict) -> dict:
        if self.mail is None:
            raise GraphError("there is no calendar connection")
        return self.mail.cancel_event(payload["event_id"], payload["comment"])

    def _read_calendar(self, payload: dict) -> dict:
        if self.mail is None:
            return {"connected": False, "busy": []}
        busy = self.mail.busy_periods(datetime.fromisoformat(payload["start"]), datetime.fromisoformat(payload["end"]))
        return {"connected": True, "busy": [[a.isoformat(), b.isoformat()] for a, b in busy]}

    def _hold_mail(self, payload: dict) -> dict:
        outbox_id = self.db.add_to_outbox(
            payload["to"], payload["subject"], payload["body"], payload.get("in_reply_to")
        )
        return {
            "sent": False,
            "outbox_id": outbox_id,
            "note": "Microsoft Graph is not connected; the approved reply is held in the local outbox.",
        }

    def _read_client_file(self, payload: dict) -> dict:
        scope = ClientScope(payload["client"])
        raw = self.clients.read_bytes(scope, payload["path"])
        parsed = parse_document(raw, payload["path"])
        text = parsed.pop("text")
        return {**parsed, "path": payload["path"], "text": text[:8000], "truncated": len(text) > 8000}

    def node_runner(self, tracker: BudgetTracker, run_id: str | None = None) -> NodeRunner:
        """With a run id, every model call is written to that run's trace."""
        if run_id is None:
            return NodeRunner(self.gateway, tracker)

        def record(entry: dict) -> None:
            self.db.record_model_call(
                run_id, {**entry, "input": tracing.clip(entry["input"]), "output": tracing.clip(entry["output"])}
            )

        # A restricted client's material goes only to providers approved for
        # confidential work. Read at every call, so setting the flag takes
        # effect on a run already under way.
        run = self.db.get_run(run_id)
        restricted = bool(run) and self.db.is_restricted(run["client"])
        providers = set(self.settings.confidential_providers) if restricted else None
        return NodeRunner(self.gateway, tracker, record, providers)


def default_contracts() -> dict[str, StepContract]:
    return {
        "draft": StepContract(
            node="draft",
            tier="standard",
            allowed_tools=[],
            input_schema=DraftIn,
            output_schema=DraftOut,
            instruction=(
                "Draft the message the owner would send. Respond as a single JSON object with "
                "keys message_subject, message_body, questions."
            ),
        ),
        "release": StepContract(
            node="release",
            tier="standard",  # release nodes run without a model; the tier is unused
            allowed_tools=["send_test_message"],
            input_schema=DraftIn,
            output_schema=DraftOut,
            instruction="",
        ),
    }


def _test_outputs(state: dict) -> dict | None:
    if not state.get("draft_subject"):
        return None
    return {
        "subject": state.get("draft_subject"),
        "body": state.get("draft_body"),
        "questions": state.get("questions", []),
        "released": state.get("released"),
    }


@dataclass(frozen=True)
class Workflow:
    build: Callable  # (Harness) -> StateGraph
    outputs: Callable[[dict], dict | None]  # graph state -> the run record's outputs
    on_rejected: Callable | None = None  # (db, run, comment) when the owner rejects
    revises: bool = False  # a send-back resumes the graph with the owner's comment


WORKFLOWS: dict[str, Workflow] = {
    "foundation-test": Workflow(build_test_graph, _test_outputs),
    "brief-evaluation": Workflow(build_brief_graph, brief_outputs),
    "sponsorship-triage": Workflow(build_sponsorship_graph, sponsorship_outputs, sponsorship_rejected),
    "creative-review": Workflow(build_creative_graph, creative_outputs),
    "strategy-drafting": Workflow(build_drafting_graph, drafting_outputs, revises=True),
    "call-change": Workflow(build_call_change_graph, call_change_outputs),
}


class UnknownWorkflow(ValueError):
    pass


def postgres_conninfo(database_url: str) -> str | None:
    """The libpq form of a SQLAlchemy Postgres URL; None for any other database."""
    url = make_url(database_url)
    if url.get_backend_name() != "postgresql":
        return None
    return url.set(drivername="postgresql").render_as_string(hide_password=False)


def open_checkpointer(settings: Settings):
    """LangGraph checkpoints sit beside the app tables: in Postgres when the
    database is Postgres (the VPS), otherwise in the SQLite checkpoint file."""
    conninfo = postgres_conninfo(settings.database_url)
    if conninfo is None:
        conn = sqlite3.connect(settings.checkpoint_path, check_same_thread=False)
        return SqliteSaver(conn)
    # Imported here: these come with the deploy extra, which local runs do not need.
    from langgraph.checkpoint.postgres import PostgresSaver
    from psycopg.rows import dict_row
    from psycopg_pool import ConnectionPool

    pool = ConnectionPool(
        conninfo,
        min_size=1,
        max_size=5,
        open=True,
        # What PostgresSaver requires of its connections.
        kwargs={"autocommit": True, "prepare_threshold": 0, "row_factory": dict_row},
    )
    saver = PostgresSaver(pool)
    saver.setup()  # creates or migrates the checkpoint tables; safe to repeat
    return saver


class Engine:
    def __init__(self, settings: Settings, db: Database, gateway: ModelGateway | None = None,
                 mail: GraphMail | None = None):
        self.settings = settings
        self.db = db
        _, skills = load_library(settings.skills_dir)
        self.harness = Harness(
            settings=settings,
            db=db,
            gateway=gateway or ModelGateway(settings.route_table),
            contracts={
                **default_contracts(), **brief_contracts(), **sponsorship_contracts(), **creative_contracts(),
                **drafting_contracts(), **call_change_contracts(),
            },
            skill_text=skills.get("foundation-test", ""),
            skills=skills,
            mail=mail or graph_from_settings(settings),
        )
        self.checkpointer = open_checkpointer(settings)
        self._compile_graphs()

    def _compile_graphs(self) -> None:
        self.graphs = {
            name: wf.build(self.harness).compile(checkpointer=self.checkpointer)
            for name, wf in WORKFLOWS.items()
        }

    def reload_library(self) -> str:
        """Read the skill library again after an accepted change; returns its version.
        The graphs hold the skill text they were built with, so they are rebuilt."""
        version, skills = load_library(self.settings.skills_dir)
        self.harness.skills = skills
        self.harness.skill_text = skills.get("foundation-test", "")
        self._compile_graphs()
        return version

    def _graph(self, run_id: str):
        run = self.db.get_run(run_id)
        if not run or run["workflow"] not in self.graphs:
            raise UnknownWorkflow(f"run {run_id} has no known workflow")
        return self.graphs[run["workflow"]]

    # --- lifecycle -----------------------------------------------------------
    def check_monthly_cap(self, by_hand: bool, what: str) -> None:
        """At the cap, only a run the owner starts by hand may begin."""
        cap = costs.current_cap(self.db, self.settings)
        if cap["state"] == "stopped" and not by_hand:
            self.db.audit(None, "run_refused_monthly_cap", {"what": what, "cap_usd": cap["cap_usd"]})
            raise costs.MonthlyCapReached(
                f"the monthly cap of ${cap['cap_usd']:.2f} is reached; {what} was not started"
            )

    def _note_cap_alert(self) -> None:
        # Once a month, when spend first reaches 80 percent of the cap.
        cap = costs.current_cap(self.db, self.settings)
        _, month_start, _ = costs.month_bounds()
        if cap["state"] in ("alert", "stopped") and not self.db.audited_since("monthly_cap_alert", month_start):
            self.db.audit(None, "monthly_cap_alert", {"cap_usd": cap["cap_usd"], "used": cap["used"]})

    def start_run(self, workflow: str, client: str, inputs: dict, by_hand: bool = True) -> str:
        """by_hand: the owner started it. A run a watcher or a schedule starts
        is refused once the month's spend reaches the cap."""
        if workflow not in WORKFLOWS:
            raise UnknownWorkflow(f"unknown workflow {workflow!r}; known: {', '.join(WORKFLOWS)}")
        self.check_monthly_cap(by_hand, f"a {workflow} run")
        run_id = new_id()
        scope = self.harness.clients.ensure(client)  # one client scope, set here, never changed
        client = scope.name
        version, _ = load_library(self.settings.skills_dir)
        self.db.create_run(run_id, workflow, client, inputs, version)
        self.db.audit(run_id, "run_created", {"workflow": workflow, "client": client})
        self._invoke(run_id, None, {"run_id": run_id, "client": client, "inputs": inputs})
        return run_id

    def approve(self, run_id: str, decision: str, comment: str | None, edited_body: str | None = None) -> dict:
        run = self.db.get_run(run_id)
        if not run:
            raise KeyError(run_id)
        if run["status"] != "waiting_for_approval":
            raise ValueError(f"run {run_id} is {run['status']}, not waiting for approval")
        workflow = WORKFLOWS[run["workflow"]]

        pending = self.pending_approval(run_id)
        approved_hash = pending["content_hash"]
        if edited_body is not None:
            # Edit then approve: the approval is for the owner's version, and the
            # difference from the draft is kept for the learning loop.
            if decision != "approved" or not pending.get("editable"):
                raise ValueError("only an editable item can be approved with edits")
            before = pending["content"].get("body", "")
            if edited_body == before:
                edited_body = None
            else:
                approved_hash = content_hash({**pending["content"], "body": edited_body})
                self.db.record_owner_edit(run_id, run["workflow"], pending.get("stage"), before, edited_body)
        approval_id = self.db.record_approval(run_id, approved_hash, decision, comment)
        self.db.audit(run_id, "approval_recorded", {
            "decision": decision, "content_hash": approved_hash, "edited": edited_body is not None,
        })

        if decision == "approved":
            resume = {"approval_id": approval_id}
            if edited_body is not None:
                resume["edited_body"] = edited_body
            self._invoke(run_id, Command(resume=resume), None)
        elif decision == "rejected":
            self.db.update_run(run_id, status="cancelled", error_reason=f"rejected by owner: {comment}")
            if workflow.on_rejected:
                workflow.on_rejected(self.db, run, comment)
        else:
            self.db.audit(run_id, "sent_back", {"comment": comment})
            if workflow.revises:  # a revision request starts a new pass with the owner's comment
                self._invoke(run_id, Command(resume={"approval_id": approval_id}), None)
        return self.db.get_run(run_id)

    def forget_run(self, run_id: str, marker: dict) -> None:
        """Remove a run's payloads and its checkpoints (which hold the same
        material as graph state). The run row and its cost remain."""
        self.checkpointer.delete_thread(run_id)
        self.db.delete_run_payloads(run_id, marker)

    def provide_input(self, run_id: str, answers: str) -> dict:
        """The owner's answers to a run that asked questions and is waiting."""
        run = self.db.get_run(run_id)
        if not run:
            raise KeyError(run_id)
        if run["status"] != "waiting_for_input":
            raise ValueError(f"run {run_id} is {run['status']}, not waiting for input")
        # The answers are client material and live in the run; the audit log, which is
        # never erased, records only that they were given.
        self.db.audit(run_id, "input_provided", {"characters": len(answers)})
        self._invoke(run_id, Command(resume={"answers": answers}), None)
        return self.db.get_run(run_id)

    # --- inspection ------------------------------------------------------------
    def _waiting_on(self, run_id: str) -> dict | None:
        """What an interrupted run is waiting for: an approval or an input request."""
        snap = self._graph(run_id).get_state({"configurable": {"thread_id": run_id}})
        for task in snap.tasks:
            for intr in task.interrupts or ():
                if isinstance(intr.value, dict) and intr.value.get("type") in ("approval", "input"):
                    return intr.value
        return None

    def pending_approval(self, run_id: str) -> dict:
        value = self._waiting_on(run_id)
        if not value or value["type"] != "approval":
            raise ValueError(f"run {run_id} has no pending approval request")
        return value

    def input_requests(self) -> list[dict]:
        items = []
        for run in self.db.list_runs(status="waiting_for_input"):
            try:
                value = self._waiting_on(run["id"])
            except ValueError:
                continue
            if value and value["type"] == "input":
                items.append({
                    "run_id": run["id"], "workflow": run["workflow"], "client": run["client"],
                    "goal": value.get("goal", ""), "questions": value.get("questions", []),
                })
        return items

    def queue(self) -> list[dict]:
        items = []
        for run in self.db.list_runs(status="waiting_for_approval"):
            try:
                pending = self.pending_approval(run["id"])
            except ValueError:
                continue
            items.append(
                {
                    "run_id": run["id"],
                    "workflow": run["workflow"],
                    "client": run["client"],
                    "content": pending["content"],
                    "content_hash": pending["content_hash"],
                    "error": pending.get("error"),
                    # What approving does, and what the owner should know beside the content.
                    "action": pending.get("action", "release"),
                    "notes": pending.get("notes", []),
                    "editable": pending.get("editable", False),
                }
            )
        return items

    # --- internals ---------------------------------------------------------------
    def _invoke(self, run_id: str, command, start_state: dict | None) -> None:
        # A drafting run takes one graph step per section; every loop is bounded in the graph itself.
        config = {"configurable": {"thread_id": run_id}, "recursion_limit": 80}
        graph = self._graph(run_id)
        self.db.update_run(run_id, status="running")
        clock = time.monotonic()

        def open_step(status: str, error: str | None = None) -> None:
            # The node that did not finish: the one that failed, or the one waiting on the owner.
            pending = graph.get_state(config).next
            if pending:
                self.db.record_step(
                    run_id, pending[0], status, None, int((time.monotonic() - clock) * 1000), error
                )

        try:
            # Streamed so each node's finish is written to the trace as it happens.
            for chunk in graph.stream(command if command is not None else start_state, config=config, stream_mode="updates"):
                elapsed, clock = int((time.monotonic() - clock) * 1000), time.monotonic()
                for node, update in chunk.items():
                    if node != "__interrupt__":
                        self.db.record_step(run_id, node, "done", tracing.clip(update), elapsed)
        except (StepFailed, BudgetExceeded) as e:
            open_step("failed", str(e))
            self._sync(run_id, status_override="failed", error_reason=str(e))
            return
        except Exception as e:  # provider or unexpected errors: stop with a clear reason, keep partial work
            open_step("failed", f"{type(e).__name__}: {e}")
            self._sync(run_id, status_override="failed", error_reason=f"{type(e).__name__}: {e}")
            return
        open_step("waiting")
        self._sync(run_id)

    def _sync(self, run_id: str, status_override: str | None = None, error_reason: str | None = None) -> None:
        snap = self._graph(run_id).get_state({"configurable": {"thread_id": run_id}})
        state = snap.values or {}
        workflow = WORKFLOWS[self.db.get_run(run_id)["workflow"]]
        usage = state.get("usage") or {}
        flags = state.get("flags", [])

        if status_override:
            status, reason = status_override, error_reason
        elif snap.next:  # interrupted mid-graph: waiting on a human
            waiting = self._waiting_on(run_id)
            status = "waiting_for_input" if waiting and waiting["type"] == "input" else "waiting_for_approval"
            reason = None
        else:
            status = "failed" if state.get("error") else "done"
            reason = state.get("error")

        self.db.update_run(
            run_id,
            status=status,
            error_reason=reason,
            outputs=workflow.outputs(state),
            tokens_in=usage.get("tokens_in", 0),
            tokens_out=usage.get("tokens_out", 0),
            cost_usd=usage.get("cost_usd", 0.0),
            fallback_used=bool(state.get("fallback_used")),
            flags=flags,
        )
        self._note_cap_alert()
