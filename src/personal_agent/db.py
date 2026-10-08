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
