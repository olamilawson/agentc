"""App state tables: runs, approvals, audit log, tool calls.

SQLAlchemy Core so the same code runs on SQLite (local tests) and Postgres
(VPS). The audit log is insert-only — nothing in this module ever updates or
deletes from it.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import (
    Boolean,
    Column,
    Float,
    Integer,
    MetaData,
    String,
    Table,
    create_engine,
    delete,
    func,
    insert,
    or_,
    select,
    update,
)
from sqlalchemy.types import JSON

metadata = MetaData()

runs = Table(
    "runs",
    metadata,
    Column("id", String, primary_key=True),
    Column("workflow", String, nullable=False),
    Column("client", String, nullable=False),
    Column("status", String, nullable=False, index=True),
    Column("inputs", JSON),
    Column("outputs", JSON),
    Column("error_reason", String),
    Column("skill_library_version", String),
    Column("tokens_in", Integer, default=0),
    Column("tokens_out", Integer, default=0),
    Column("cost_usd", Float, default=0.0),
    Column("fallback_used", Boolean, default=False),
    Column("flags", JSON, default=list),
    Column("created_at", String),
    Column("updated_at", String),
)

approvals = Table(
    "approvals",
    metadata,
    Column("id", String, primary_key=True),
    Column("run_id", String, nullable=False, index=True),
    Column("content_hash", String, nullable=False),
    Column("decision", String, nullable=False),  # approved | rejected | sent_back
    Column("comment", String),
    Column("decided_by", String, default="owner"),
    Column("lapsed", Boolean, default=False),
    Column("created_at", String),
)

audit_log = Table(
    "audit_log",
    metadata,
    Column("id", String, primary_key=True),
    Column("run_id", String, index=True),
    Column("action", String, nullable=False),
    Column("detail", JSON),
    Column("created_at", String),
)

tool_calls = Table(
    "tool_calls",
    metadata,
    Column("id", String, primary_key=True),
    Column("run_id", String, index=True),
    Column("node", String),
    Column("tool", String),
    Column("tool_class", String),
    Column("payload_hash", String),
    Column("idempotency_key", String, index=True),
    Column("outcome", String),  # executed | refused | awaiting_approval | replay | uncertain
    Column("detail", JSON),
    Column("created_at", String),
)


# One row per sponsorship enquiry. Written by the agent after each decision;
# the owner can correct any row.
sponsorship_ledger = Table(
    "sponsorship_ledger",
    metadata,
    Column("id", String, primary_key=True),
    Column("run_id", String, index=True),
    Column("message_id", String, index=True),
    Column("received_at", String),
    Column("sender", String, index=True),
    Column("organisation", String),
    Column("request", String),
    Column("amount", String),
    Column("route", String),
    Column("decision", String),
    Column("outcome", String),
    Column("created_at", String),
    Column("updated_at", String),
)
LEDGER_FIELDS = ("received_at", "sender", "organisation", "request", "amount", "route", "decision", "outcome")

# Approved mail held locally until Microsoft Graph is connected. Nothing in
# this table has been delivered to anyone.
outbox = Table(
    "outbox",
    metadata,
    Column("id", String, primary_key=True),
    Column("recipient", String, nullable=False),
    Column("subject", String),
    Column("body", String),
    Column("in_reply_to", String),
    Column("created_at", String),
)


# What the owner has set for a client. A client is its folder; this holds only
# the settings that are not files.
client_settings = Table(
    "client_settings",
    metadata,
    Column("name", String, primary_key=True),
    Column("retention_days", Integer),  # None: keep until the owner deletes
    Column("updated_at", String),
)

# Calls the agent has put in the owner's calendar, so a later mail from the
# same sender can move or cancel the right event.
calls = Table(
    "calls",
    metadata,
    Column("id", String, primary_key=True),
    Column("run_id", String, index=True),  # the run that created the event
    Column("event_id", String),  # Microsoft Graph's id for the event
    Column("sender", String, index=True),
    Column("subject", String),
    Column("start", String),
    Column("end", String),
    Column("label", String),
    Column("status", String),  # scheduled | cancelled
    Column("created_at", String),
    Column("updated_at", String),
)

# Clients whose runs may only reach providers approved for confidential work.
# --- the owner's workspace: tasks, decisions, memory and the watchlist ---------------------
# These are the owner's own records. The agent reads workstream knowledge as
# context and writes none of them.
tasks = Table(
    "tasks",
    metadata,
    Column("id", String, primary_key=True),
    Column("workstream", String, nullable=False, index=True),
    Column("title", String, nullable=False),
    Column("detail", String),
    Column("status", String, nullable=False, index=True),  # todo | in_progress | awaiting_approval | done
    Column("risk", String),  # low | medium | high
    Column("owner", String),
    Column("due", String),  # the owner's local time, YYYY-MM-DDTHH:MM
    Column("decision", String),  # approved | changes | declined
    Column("created_at", String),
    Column("updated_at", String),
)

# What the owner decided, and why: for a task, or for an agent run's draft.
decisions = Table(
    "decisions",
    metadata,
    Column("id", String, primary_key=True),
    Column("workstream", String, index=True),
    Column("title", String),
    Column("decision", String),  # approved | changes | declined
    Column("reason", String),
    Column("source", String),  # task | run
    Column("ref", String),
    Column("created_at", String),
)

# Workstream knowledge (priority, brand rule, preference, lesson) and saved drafts.
memory_items = Table(
    "memory_items",
    metadata,
    Column("id", String, primary_key=True),
    Column("workstream", String, index=True),
    Column("kind", String, index=True),  # priority | brand_rule | preference | lesson | draft
    Column("title", String),
    Column("text", String),
    Column("created_at", String),
)

# The owner's watchlist. Each signal is entered by the owner, with its source.
signals = Table(
    "signals",
    metadata,
    Column("id", String, primary_key=True),
    Column("workstream", String, index=True),
    Column("kind", String, index=True),  # reputation | regulation | opportunity
    Column("title", String),
    Column("note", String),
    Column("source", String),
    Column("created_at", String),
)

# A brief approved for an agency. It is held until release_at, then sent; the
# owner can recall it while it is held.
dispatches = Table(
    "dispatches",
    metadata,
    Column("id", String, primary_key=True),
    Column("run_id", String, index=True),
    Column("workstream", String, index=True),
    Column("kind", String),  # brief | halt
    Column("agency", String),
    Column("recipient", String),
    Column("subject", String),
    Column("body", String),
    Column("attachments", JSON),  # paths in the workstream's folder
    Column("directive", String),
    Column("revision_of", String),  # the dispatch this one replaces
    Column("halts", String),  # for a halt notice: the dispatch it asks the agency to stop
    Column("status", String, index=True),  # held | sending | sent | outbox | recalled | halt_pending | halted | failed | uncertain
    Column("note", String),
    Column("approved_hash", String),
    Column("release_at", String),
    Column("sent_at", String),
    Column("recalled_at", String),
    Column("created_at", String),
)

# The owner's choice of model for a tier, made on the Settings screen. A row
# replaces that tier's line in config/route_table.yaml; no row leaves the file's.
model_routes = Table(
    "model_routes",
    metadata,
    Column("tier", String, primary_key=True),
    Column("primary", String, nullable=False),
    Column("fallback", String),
    Column("updated_at", String),
)

# A row means restricted; no row means not.
restricted_clients = Table(
    "restricted_clients",
    metadata,
    Column("name", String, primary_key=True),
    Column("since", String),
)

# The trace of a run (PRD: tracing in run tables). One row each time a graph
# node finishes, fails or stops to wait for the owner, in order.
run_steps = Table(
    "run_steps",
    metadata,
    Column("id", String, primary_key=True),
    Column("run_id", String, index=True),
    Column("seq", Integer),
    Column("node", String),
    Column("status", String),  # done | failed | waiting
    Column("output", JSON),  # what the node wrote to the run's state, long text shortened
    Column("error", String),
    Column("duration_ms", Integer),
    Column("created_at", String),
)

# One row per model call, including calls whose output was invalid and retried.
model_calls = Table(
    "model_calls",
    metadata,
    Column("id", String, primary_key=True),
    Column("run_id", String, index=True),
    Column("step_seq", Integer),  # the run step the call was made in
    Column("node", String),
    Column("attempt", Integer),
    Column("tier", String),
    Column("model", String),
    Column("fallback_used", Boolean),
    Column("prompt_version", String),  # hash of the node's instruction
    Column("tokens_in", Integer),
    Column("tokens_out", Integer),
    Column("cost_usd", Float),
    Column("outcome", String),  # ok | invalid output | provider error
    Column("error", String),
    Column("input", JSON),
    Column("output", JSON),
    Column("created_at", String),
)

# Changes to the skill library proposed by the weekly learning review. Nothing
# here changes a skill file until the owner accepts the row.
skill_proposals = Table(
    "skill_proposals",
    metadata,
    Column("id", String, primary_key=True),
    Column("review_run_id", String, index=True),
    Column("skill", String, nullable=False),
    Column("rationale", String),
    Column("evidence", JSON),  # run ids the proposal rests on
    Column("find", String),  # exact text in the skill file; empty means append
    Column("replace", String),
    Column("status", String, nullable=False, index=True),  # pending | accepted | declined | stale
    Column("decision_note", String),
    Column("commit", String),
    Column("library_version", String),  # the library version the accepted change produced
    Column("created_at", String),
    Column("decided_at", String),
)

# What the owner changed before approving: the draft and the approved version.
# The weekly learning review reads these; nothing else acts on them.
owner_edits = Table(
    "owner_edits",
    metadata,
    Column("id", String, primary_key=True),
    Column("run_id", String, index=True),
    Column("workflow", String, index=True),
    Column("stage", String),
    Column("before", String),
    Column("after", String),
    Column("created_at", String),
)


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def new_id() -> str:
    return uuid.uuid4().hex


class Database:
    def __init__(self, url: str):
        self.engine = create_engine(url)
        metadata.create_all(self.engine)

    # --- audit (append-only) -------------------------------------------------
    def audit(self, run_id: str | None, action: str, detail: dict | None = None) -> None:
        with self.engine.begin() as cx:
            cx.execute(
                insert(audit_log).values(
                    id=new_id(), run_id=run_id, action=action, detail=detail, created_at=now()
                )
            )

    def audited_since(self, action: str, since: str) -> bool:
        with self.engine.connect() as cx:
            return cx.execute(
                select(audit_log.c.id).where(audit_log.c.action == action, audit_log.c.created_at >= since).limit(1)
            ).first() is not None

    # --- runs ----------------------------------------------------------------
    def create_run(self, run_id: str, workflow: str, client: str, inputs: dict, library_version: str) -> None:
        with self.engine.begin() as cx:
            cx.execute(
                insert(runs).values(
                    id=run_id,
                    workflow=workflow,
                    client=client,
                    status="queued",
                    inputs=inputs,
                    skill_library_version=library_version,
                    tokens_in=0,
                    tokens_out=0,
                    cost_usd=0.0,
                    fallback_used=False,
                    flags=[],
                    created_at=now(),
                    updated_at=now(),
                )
            )

    def update_run(self, run_id: str, **fields) -> None:
        with self.engine.begin() as cx:
            cx.execute(update(runs).where(runs.c.id == run_id).values(**fields, updated_at=now()))

    def get_run(self, run_id: str) -> dict | None:
        with self.engine.connect() as cx:
            row = cx.execute(select(runs).where(runs.c.id == run_id)).mappings().first()
        return dict(row) if row else None

    def list_runs(self, status: str | None = None) -> list[dict]:
        stmt = select(runs).order_by(runs.c.created_at.desc())
        if status:
            stmt = stmt.where(runs.c.status == status)
        with self.engine.connect() as cx:
            return [dict(r) for r in cx.execute(stmt).mappings()]

    # --- approvals -----------------------------------------------------------
    def record_approval(self, run_id: str, content_hash: str, decision: str, comment: str | None) -> str:
        approval_id = new_id()
        with self.engine.begin() as cx:
            cx.execute(
                insert(approvals).values(
                    id=approval_id,
                    run_id=run_id,
                    content_hash=content_hash,
                    decision=decision,
                    comment=comment,
                    decided_by="owner",
                    lapsed=False,
                    created_at=now(),
                )
            )
        return approval_id

    def get_approval(self, approval_id: str) -> dict | None:
        with self.engine.connect() as cx:
            row = cx.execute(select(approvals).where(approvals.c.id == approval_id)).mappings().first()
        return dict(row) if row else None

    def lapse_approval(self, approval_id: str) -> None:
        with self.engine.begin() as cx:
            cx.execute(update(approvals).where(approvals.c.id == approval_id).values(lapsed=True))

    def latest_approval_for_hash(self, run_id: str, content_hash: str) -> dict | None:
        with self.engine.connect() as cx:
            row = (
                cx.execute(
                    select(approvals)
                    .where(
                        approvals.c.run_id == run_id,
                        approvals.c.content_hash == content_hash,
                        approvals.c.decision == "approved",
                        approvals.c.lapsed.is_(False),
                    )
                    .order_by(approvals.c.created_at.desc())
                )
                .mappings()
                .first()
            )
        return dict(row) if row else None

    # --- client settings, retention and deletion ---------------------------------
    def get_retention_days(self, client: str) -> int | None:
        with self.engine.connect() as cx:
            row = cx.execute(select(client_settings).where(client_settings.c.name == client)).mappings().first()
        return row["retention_days"] if row else None

    def set_retention_days(self, client: str, days: int | None) -> None:
        with self.engine.begin() as cx:
            cx.execute(delete(client_settings).where(client_settings.c.name == client))
            cx.execute(insert(client_settings).values(name=client, retention_days=days, updated_at=now()))

    # --- the owner's workspace -----------------------------------------------
    def _insert(self, table: Table, **fields) -> str:
        row_id = new_id()
        with self.engine.begin() as cx:
            cx.execute(insert(table).values(id=row_id, created_at=now(), **fields))
        return row_id

    def _rows(self, table: Table, **where) -> list[dict]:
        stmt = select(table).order_by(table.c.created_at.desc())
        for column, value in where.items():
            stmt = stmt.where(table.c[column] == value)
        with self.engine.connect() as cx:
            return [dict(r) for r in cx.execute(stmt).mappings()]

    def _remove(self, table: Table, row_id: str) -> None:
        with self.engine.begin() as cx:
            cx.execute(delete(table).where(table.c.id == row_id))

    def create_task(self, **fields) -> str:
        return self._insert(tasks, updated_at=now(), **fields)

    def update_task(self, task_id: str, **fields) -> None:
        with self.engine.begin() as cx:
            cx.execute(update(tasks).where(tasks.c.id == task_id).values(updated_at=now(), **fields))

    def get_task(self, task_id: str) -> dict | None:
        return next(iter(self._rows(tasks, id=task_id)), None)

    def list_tasks(self) -> list[dict]:
        return self._rows(tasks)

    def delete_task(self, task_id: str) -> None:
        self._remove(tasks, task_id)

    def record_decision(self, workstream: str, title: str, decision: str, reason: str | None, source: str, ref: str) -> str:
        return self._insert(decisions, workstream=workstream, title=title, decision=decision, reason=reason,
                            source=source, ref=ref)

    def list_decisions(self) -> list[dict]:
        return self._rows(decisions)

    def add_memory(self, workstream: str, kind: str, title: str, text: str) -> str:
        return self._insert(memory_items, workstream=workstream, kind=kind, title=title, text=text)

    def list_memory(self, workstream: str | None = None) -> list[dict]:
        return self._rows(memory_items, **({"workstream": workstream} if workstream else {}))

    def delete_memory(self, item_id: str) -> None:
        self._remove(memory_items, item_id)

    def add_signal(self, workstream: str, kind: str, title: str, note: str, source: str) -> str:
        return self._insert(signals, workstream=workstream, kind=kind, title=title, note=note, source=source)

    def list_signals(self) -> list[dict]:
        return self._rows(signals)

    def delete_signal(self, signal_id: str) -> None:
        self._remove(signals, signal_id)

    # --- agency dispatches ----------------------------------------------------
    def create_dispatch(self, **fields) -> str:
        return self._insert(dispatches, **fields)

    def get_dispatch(self, dispatch_id: str) -> dict | None:
        return next(iter(self._rows(dispatches, id=dispatch_id)), None)

    def list_dispatches(self, status: str | None = None) -> list[dict]:
        return self._rows(dispatches, **({"status": status} if status else {}))

    def update_dispatch(self, dispatch_id: str, **fields) -> None:
        with self.engine.begin() as cx:
            cx.execute(update(dispatches).where(dispatches.c.id == dispatch_id).values(**fields))

    def claim_dispatch(self, dispatch_id: str, was: str, becomes: str) -> bool:
        """Move a dispatch from one status to another, if it is still in the first.
        Two callers cannot both win, so a brief is sent once and a recall beats a send or loses to it."""
        with self.engine.begin() as cx:
            changed = cx.execute(
                update(dispatches).where(dispatches.c.id == dispatch_id, dispatches.c.status == was).values(status=becomes)
            )
        return changed.rowcount == 1

    def recent_audit(self, limit: int = 40) -> list[dict]:
        with self.engine.connect() as cx:
            return [dict(r) for r in cx.execute(
                select(audit_log).order_by(audit_log.c.created_at.desc()).limit(limit)
            ).mappings()]

    def forget_client_settings(self, client: str) -> None:
        with self.engine.begin() as cx:
            # A deleted client's tasks, decisions, knowledge and signals go with it.
            for table in (tasks, decisions, memory_items, signals, dispatches):
                cx.execute(delete(table).where(table.c.workstream == client))
            cx.execute(delete(client_settings).where(client_settings.c.name == client))
            cx.execute(delete(restricted_clients).where(restricted_clients.c.name == client))

    def model_routes(self) -> dict[str, dict]:
        with self.engine.connect() as cx:
            return {
                r["tier"]: {"primary": r["primary"], "fallback": r["fallback"]}
                for r in cx.execute(select(model_routes)).mappings()
            }

    def set_model_route(self, tier: str, primary: str, fallback: str | None) -> None:
        with self.engine.begin() as cx:
            cx.execute(delete(model_routes).where(model_routes.c.tier == tier))
            cx.execute(insert(model_routes).values(tier=tier, primary=primary, fallback=fallback, updated_at=now()))

    def is_restricted(self, client: str) -> bool:
        with self.engine.connect() as cx:
            return cx.execute(
                select(restricted_clients.c.name).where(restricted_clients.c.name == client)
            ).first() is not None

    def set_restricted(self, client: str, restricted: bool) -> None:
        with self.engine.begin() as cx:
            cx.execute(delete(restricted_clients).where(restricted_clients.c.name == client))
            if restricted:
                cx.execute(insert(restricted_clients).values(name=client, since=now()))

    def restricted_client_names(self) -> set[str]:
        with self.engine.connect() as cx:
            return {row[0] for row in cx.execute(select(restricted_clients.c.name))}

    def list_runs_for_client(self, client: str) -> list[dict]:
        with self.engine.connect() as cx:
            return [dict(r) for r in cx.execute(select(runs).where(runs.c.client == client)).mappings()]

    def delete_run_payloads(self, run_id: str, marker: dict) -> None:
        """Remove what a run held of its client's material, everywhere it is kept.

        The run row stays, with its workflow, client name, status, times, tokens
        and cost, so cost history still adds up. The audit log is append-only
        and is not touched.
        """
        with self.engine.begin() as cx:
            # An approved reply held for sending is found through its tool call.
            held = [
                (row["detail"] or {}).get("outbox_id")
                for row in cx.execute(select(tool_calls.c.detail).where(tool_calls.c.run_id == run_id)).mappings()
                if isinstance(row["detail"], dict)
            ]
            for outbox_id in filter(None, held):
                cx.execute(delete(outbox).where(outbox.c.id == outbox_id))
            cx.execute(
                update(runs).where(runs.c.id == run_id)
                .values(inputs=None, outputs=marker, error_reason=None, flags=[], updated_at=now())
            )
            cx.execute(update(run_steps).where(run_steps.c.run_id == run_id).values(output=None, error=None))
            cx.execute(
                update(model_calls).where(model_calls.c.run_id == run_id).values(input=None, output=None, error=None)
            )
            cx.execute(update(tool_calls).where(tool_calls.c.run_id == run_id).values(detail=None))
            cx.execute(update(approvals).where(approvals.c.run_id == run_id).values(comment=None))
            cx.execute(delete(owner_edits).where(owner_edits.c.run_id == run_id))
            cx.execute(delete(sponsorship_ledger).where(sponsorship_ledger.c.run_id == run_id))
            cx.execute(delete(calls).where(calls.c.run_id == run_id))

    # --- scheduled calls ---------------------------------------------------------------
    def create_call(self, run_id: str, event_id: str, sender: str, subject: str, start: str, end: str,
                    label: str) -> str:
        call_id = new_id()
        with self.engine.begin() as cx:
            cx.execute(
                insert(calls).values(
                    id=call_id, run_id=run_id, event_id=event_id, sender=sender, subject=subject, start=start,
                    end=end, label=label, status="scheduled", created_at=now(), updated_at=now(),
                )
            )
        return call_id

    def update_call(self, call_id: str, **fields) -> None:
        with self.engine.begin() as cx:
            cx.execute(update(calls).where(calls.c.id == call_id).values(**fields, updated_at=now()))

    def get_call(self, call_id: str) -> dict | None:
        with self.engine.connect() as cx:
            row = cx.execute(select(calls).where(calls.c.id == call_id)).mappings().first()
        return dict(row) if row else None

    def list_calls(self) -> list[dict]:
        with self.engine.connect() as cx:
            return [dict(r) for r in cx.execute(select(calls).order_by(calls.c.start.desc())).mappings()]

    def scheduled_call_for(self, sender: str, after: str) -> dict | None:
        """The next call still scheduled with this sender that starts after the given moment."""
        stmt = (
            select(calls)
            .where(func.lower(calls.c.sender) == sender.lower(), calls.c.status == "scheduled", calls.c.start > after)
            .order_by(calls.c.start)
        )
        with self.engine.connect() as cx:
            row = cx.execute(stmt).mappings().first()
        return dict(row) if row else None

    # --- trace: run steps and model calls ------------------------------------------
    def _next_seq(self, cx, run_id: str) -> int:
        return cx.execute(
            select(func.count()).select_from(run_steps).where(run_steps.c.run_id == run_id)
        ).scalar_one() + 1

    def record_step(self, run_id: str, node: str, status: str, output: dict | None,
                    duration_ms: int | None, error: str | None = None) -> None:
        with self.engine.begin() as cx:
            cx.execute(
                insert(run_steps).values(
                    id=new_id(), run_id=run_id, seq=self._next_seq(cx, run_id), node=node, status=status,
                    output=output, error=error, duration_ms=duration_ms, created_at=now(),
                )
            )

    def record_model_call(self, run_id: str, entry: dict) -> None:
        with self.engine.begin() as cx:
            # A call belongs to the step now running: the one after the last recorded.
            cx.execute(
                insert(model_calls).values(
                    id=new_id(), run_id=run_id, step_seq=self._next_seq(cx, run_id), created_at=now(), **entry
                )
            )

    def list_steps(self, run_id: str) -> list[dict]:
        with self.engine.connect() as cx:
            return [dict(r) for r in cx.execute(
                select(run_steps).where(run_steps.c.run_id == run_id).order_by(run_steps.c.seq)
            ).mappings()]

    def list_model_calls(self, run_id: str) -> list[dict]:
        with self.engine.connect() as cx:
            return [dict(r) for r in cx.execute(
                select(model_calls).where(model_calls.c.run_id == run_id)
                .order_by(model_calls.c.step_seq, model_calls.c.created_at, model_calls.c.attempt)
            ).mappings()]

    def list_tool_calls(self, run_id: str) -> list[dict]:
        with self.engine.connect() as cx:
            return [dict(r) for r in cx.execute(
                select(tool_calls).where(tool_calls.c.run_id == run_id).order_by(tool_calls.c.created_at)
            ).mappings()]

    # --- owner edits (for the learning loop) -------------------------------------
    def record_owner_edit(self, run_id: str, workflow: str, stage: str | None, before: str, after: str) -> None:
        with self.engine.begin() as cx:
            cx.execute(
                insert(owner_edits).values(
                    id=new_id(), run_id=run_id, workflow=workflow, stage=stage,
                    before=before, after=after, created_at=now(),
                )
            )

    def list_owner_edits(self, run_id: str | None = None) -> list[dict]:
        stmt = select(owner_edits).order_by(owner_edits.c.created_at)
        if run_id:
            stmt = stmt.where(owner_edits.c.run_id == run_id)
        with self.engine.connect() as cx:
            return [dict(r) for r in cx.execute(stmt).mappings()]

    def approvals_between(self, since: str, until: str) -> list[dict]:
        """Every owner decision in the period, with its run's workflow and client."""
        stmt = (
            select(approvals, runs.c.workflow, runs.c.client)
            .join(runs, runs.c.id == approvals.c.run_id)
            .where(approvals.c.created_at >= since, approvals.c.created_at < until)
            .order_by(approvals.c.created_at)
        )
        with self.engine.connect() as cx:
            return [dict(r) for r in cx.execute(stmt).mappings()]

    def owner_edits_between(self, since: str, until: str) -> list[dict]:
        stmt = (
            select(owner_edits)
            .where(owner_edits.c.created_at >= since, owner_edits.c.created_at < until)
            .order_by(owner_edits.c.created_at)
        )
        with self.engine.connect() as cx:
            return [dict(r) for r in cx.execute(stmt).mappings()]

    def runs_between(self, since: str, until: str) -> list[dict]:
        stmt = select(runs).where(runs.c.created_at >= since, runs.c.created_at < until)
        with self.engine.connect() as cx:
            return [dict(r) for r in cx.execute(stmt).mappings()]

    # --- skill proposals (the learning loop) ---------------------------------------
    def create_proposal(self, review_run_id: str, skill: str, rationale: str, evidence: list[str],
                        find: str, replace: str) -> str:
        proposal_id = new_id()
        with self.engine.begin() as cx:
            cx.execute(
                insert(skill_proposals).values(
                    id=proposal_id, review_run_id=review_run_id, skill=skill, rationale=rationale,
                    evidence=evidence, find=find, replace=replace, status="pending", created_at=now(),
                )
            )
        return proposal_id

    def get_proposal(self, proposal_id: str) -> dict | None:
        with self.engine.connect() as cx:
            row = cx.execute(
                select(skill_proposals).where(skill_proposals.c.id == proposal_id)
            ).mappings().first()
        return dict(row) if row else None

    def list_proposals(self, status: str | None = None) -> list[dict]:
        stmt = select(skill_proposals).order_by(skill_proposals.c.created_at.desc())
        if status:
            stmt = stmt.where(skill_proposals.c.status == status)
        with self.engine.connect() as cx:
            return [dict(r) for r in cx.execute(stmt).mappings()]

    def decide_proposal(self, proposal_id: str, status: str, **fields) -> None:
        with self.engine.begin() as cx:
            cx.execute(
                update(skill_proposals)
                .where(skill_proposals.c.id == proposal_id)
                .values(status=status, decided_at=now(), **fields)
            )

    # --- sponsorship ledger ----------------------------------------------------
    def create_ledger_row(self, run_id: str, **fields) -> str:
        ledger_id = new_id()
        with self.engine.begin() as cx:
            cx.execute(
                insert(sponsorship_ledger).values(
                    id=ledger_id, run_id=run_id, created_at=now(), updated_at=now(), **fields
                )
            )
        return ledger_id

    def update_ledger_row(self, ledger_id: str, **fields) -> None:
        with self.engine.begin() as cx:
            cx.execute(
                update(sponsorship_ledger)
                .where(sponsorship_ledger.c.id == ledger_id)
                .values(**fields, updated_at=now())
            )

    def get_ledger_row(self, ledger_id: str) -> dict | None:
        with self.engine.connect() as cx:
            row = cx.execute(
                select(sponsorship_ledger).where(sponsorship_ledger.c.id == ledger_id)
            ).mappings().first()
        return dict(row) if row else None

    def ledger_row_for_run(self, run_id: str) -> dict | None:
        with self.engine.connect() as cx:
            row = cx.execute(
                select(sponsorship_ledger).where(sponsorship_ledger.c.run_id == run_id)
            ).mappings().first()
        return dict(row) if row else None

    def list_ledger(self) -> list[dict]:
        with self.engine.connect() as cx:
            return [
                dict(r)
                for r in cx.execute(
                    select(sponsorship_ledger).order_by(sponsorship_ledger.c.created_at.desc())
                ).mappings()
            ]

    def ledger_history(self, sender: str, organisation: str | None, exclude_run_id: str) -> list[dict]:
        """Earlier enquiries from the same sender or the same organisation."""
        same = func.lower(sponsorship_ledger.c.sender) == sender.lower()
        if organisation:
            same = or_(same, func.lower(sponsorship_ledger.c.organisation) == organisation.lower())
        stmt = (
            select(sponsorship_ledger)
            .where(same, sponsorship_ledger.c.run_id != exclude_run_id)
            .order_by(sponsorship_ledger.c.created_at.desc())
            .limit(10)
        )
        with self.engine.connect() as cx:
            return [dict(r) for r in cx.execute(stmt).mappings()]

    def find_run_by_message(self, workflow: str, message_id: str) -> dict | None:
        """The run already started for a mail, so a repeated delivery starts nothing."""
        for run in self.list_runs():
            if run["workflow"] == workflow and (run["inputs"] or {}).get("message_id") == message_id:
                return run
        return None

    # --- outbox (mail approved but not delivered) --------------------------------
    def add_to_outbox(self, recipient: str, subject: str, body: str, in_reply_to: str | None) -> str:
        outbox_id = new_id()
        with self.engine.begin() as cx:
            cx.execute(
                insert(outbox).values(
                    id=outbox_id, recipient=recipient, subject=subject, body=body,
                    in_reply_to=in_reply_to, created_at=now(),
                )
            )
        return outbox_id

    def list_outbox(self) -> list[dict]:
        with self.engine.connect() as cx:
            return [dict(r) for r in cx.execute(select(outbox).order_by(outbox.c.created_at.desc())).mappings()]

    # --- tool calls ------------------------------------------------------------
    def record_tool_call(
        self, run_id: str, node: str, tool: str, tool_class: str, payload_hash: str,
        idempotency_key: str | None, outcome: str, detail: dict | None
    ) -> None:
        with self.engine.begin() as cx:
            cx.execute(
                insert(tool_calls).values(
                    id=new_id(), run_id=run_id, node=node, tool=tool, tool_class=tool_class,
                    payload_hash=payload_hash, idempotency_key=idempotency_key,
                    outcome=outcome, detail=detail, created_at=now(),
                )
            )

    def find_tool_call(self, idempotency_key: str) -> dict | None:
        with self.engine.connect() as cx:
            row = cx.execute(
                select(tool_calls).where(tool_calls.c.idempotency_key == idempotency_key)
            ).mappings().first()
        return dict(row) if row else None
