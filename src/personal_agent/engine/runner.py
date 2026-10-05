"""Engine: creates runs, drives the graph, records approvals, syncs the run record.

Run statuses (PRD): queued, running, waiting_for_approval, waiting_for_input,
done, failed, cancelled. A run that is waiting persists across restarts (its
LangGraph checkpoint is durable) and resumes from its last checkpoint.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field

from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.types import Command

from personal_agent.clients import ClientScope, ClientStore, parse_document
from personal_agent.config import Settings
from personal_agent.db import Database, new_id
from personal_agent.engine.graph import ROLE, DraftIn, DraftOut, build_test_graph
from personal_agent.gateway.tools import ToolClass, ToolGateway, ToolSpec, content_hash
from personal_agent.harness.budgets import BudgetExceeded, BudgetTracker
from personal_agent.harness.contract import StepContract, StepFailed
from personal_agent.harness.models import ModelGateway
from personal_agent.harness.runner import NodeRunner
from personal_agent.skills.loader import load_library

PENDING_STATUSES = {"waiting_for_approval", "waiting_for_input"}


@dataclass
class Harness:
    settings: Settings
    db: Database
    gateway: ModelGateway
    contracts: dict[str, StepContract]
    skill_text: str
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

    def _read_client_file(self, payload: dict) -> dict:
        scope = ClientScope(payload["client"])
        raw = self.clients.read_bytes(scope, payload["path"])
        parsed = parse_document(raw, payload["path"])
        text = parsed.pop("text")
        return {**parsed, "path": payload["path"], "text": text[:8000], "truncated": len(text) > 8000}

    def node_runner(self, tracker: BudgetTracker) -> NodeRunner:
        return NodeRunner(self.gateway, tracker)


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


class Engine:
    def __init__(self, settings: Settings, db: Database, gateway: ModelGateway | None = None):
        self.settings = settings
        self.db = db
        _, skills = load_library(settings.skills_dir)
        self.harness = Harness(
            settings=settings,
            db=db,
            gateway=gateway or ModelGateway(settings.route_table),
            contracts=default_contracts(),
            skill_text=skills.get("foundation-test", ""),
        )
        conn = sqlite3.connect(settings.checkpoint_path, check_same_thread=False)
        self.checkpointer = SqliteSaver(conn)
        self.graph = build_test_graph(self.harness).compile(checkpointer=self.checkpointer)

    # --- lifecycle -----------------------------------------------------------
    def start_run(self, workflow: str, client: str, inputs: dict) -> str:
        run_id = new_id()
        scope = self.harness.clients.ensure(client)  # one client scope, set here, never changed
        client = scope.name
        version, _ = load_library(self.settings.skills_dir)
        self.db.create_run(run_id, workflow, client, inputs, version)
        self.db.audit(run_id, "run_created", {"workflow": workflow, "client": client})
        self._invoke(run_id, None, {"run_id": run_id, "client": client, "inputs": inputs})
        return run_id

    def approve(self, run_id: str, decision: str, comment: str | None) -> dict:
        run = self.db.get_run(run_id)
        if not run:
            raise KeyError(run_id)
        if run["status"] != "waiting_for_approval":
            raise ValueError(f"run {run_id} is {run['status']}, not waiting for approval")

        pending = self.pending_approval(run_id)
        approval_id = self.db.record_approval(run_id, pending["content_hash"], decision, comment)
        self.db.audit(run_id, "approval_recorded", {"decision": decision, "content_hash": pending["content_hash"]})

        if decision == "approved":
            self._invoke(run_id, Command(resume={"approval_id": approval_id}), None)
        elif decision == "rejected":
            self.db.update_run(run_id, status="cancelled", error_reason=f"rejected by owner: {comment}")
        else:  # sent_back: comment recorded; a revision pass starts with the real workflows
            self.db.audit(run_id, "sent_back", {"comment": comment})
        return self.db.get_run(run_id)

    # --- inspection ------------------------------------------------------------
    def pending_approval(self, run_id: str) -> dict:
        snap = self.graph.get_state({"configurable": {"thread_id": run_id}})
        for task in snap.tasks:
            for intr in task.interrupts or ():
                value = intr.value
                if isinstance(value, dict) and value.get("type") == "approval":
                    return value
        raise ValueError(f"run {run_id} has no pending approval request")

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
                }
            )
        return items

    # --- internals ---------------------------------------------------------------
    def _invoke(self, run_id: str, command, start_state: dict | None) -> None:
        config = {"configurable": {"thread_id": run_id}}
        self.db.update_run(run_id, status="running")
        try:
            if command is not None:
                self.graph.invoke(command, config=config)
            else:
                self.graph.invoke(start_state, config=config)
        except (StepFailed, BudgetExceeded) as e:
            self._sync(run_id, status_override="failed", error_reason=str(e))
            return
        except Exception as e:  # provider or unexpected errors: stop with a clear reason, keep partial work
            self._sync(run_id, status_override="failed", error_reason=f"{type(e).__name__}: {e}")
            return
        self._sync(run_id)

    def _sync(self, run_id: str, status_override: str | None = None, error_reason: str | None = None) -> None:
        snap = self.graph.get_state({"configurable": {"thread_id": run_id}})
        state = snap.values or {}
        usage = state.get("usage") or {}
        flags = state.get("flags", [])

        if status_override:
            status, reason = status_override, error_reason
        elif snap.next:  # interrupted mid-graph: waiting on a human
            status = "waiting_for_approval"
            reason = None
        else:
            status = "failed" if state.get("error") else "done"
            reason = state.get("error")

        self.db.update_run(
            run_id,
            status=status,
            error_reason=reason,
            outputs=(
                {
                    "subject": state.get("draft_subject"),
                    "body": state.get("draft_body"),
                    "questions": state.get("questions", []),
                    "released": state.get("released"),
                }
                if state.get("draft_subject")
                else None
            ),
            tokens_in=usage.get("tokens_in", 0),
            tokens_out=usage.get("tokens_out", 0),
            cost_usd=usage.get("cost_usd", 0.0),
            fallback_used=bool(state.get("fallback_used")),
            flags=flags,
        )
