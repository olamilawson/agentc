"""Per-client retention and deletion (PRD: security / providers, retention and audit).

The owner sets a retention period per client. Deleting a client removes its
folder and the payloads in its run records. Both leave the run rows themselves
(workflow, client name, status, times, tokens, cost), so the month's cost still
adds up, and neither touches the audit log, which is append-only.

    python -m personal_agent.retention        # apply every client's retention period
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime, timedelta, timezone

from personal_agent.clients import ClientScope
from personal_agent.engine.runner import PENDING_STATUSES, Engine

FINISHED = ("done", "failed", "cancelled")


def _already_forgotten(run: dict) -> bool:
    return isinstance(run["outputs"], dict) and "deleted" in run["outputs"]


def set_retention(engine: Engine, client: str, days: int | None) -> dict:
    """days: how long the client's material is kept; None keeps it until deleted."""
    scope = engine.harness.clients.scope(client)  # KeyError for an unknown client
    if days is not None and days < 1:
        raise ValueError("a retention period is at least one day; leave it empty to keep everything")
    engine.db.set_retention_days(scope.name, days)
    engine.db.audit(None, "retention_set", {"client": scope.name, "days": days})  # a configuration change
    return {"client": scope.name, "retention_days": days}


def apply_retention(engine: Engine, now: datetime | None = None) -> list[dict]:
    """Remove what is older than each client's retention period: files by when
    they were last changed, run payloads by the run's last activity. A run still
    waiting on the owner is never removed here; it is reported instead."""
    now = now or datetime.now(timezone.utc)
    report = []
    for client in engine.harness.clients.list_clients():
        days = engine.db.get_retention_days(client)
        if days is None:
            continue
        cutoff = now - timedelta(days=days)
        cutoff_text = cutoff.isoformat(timespec="seconds")
        files = engine.harness.clients.delete_files_older_than(ClientScope(client), cutoff.timestamp())
        forgotten, waiting = 0, 0
        for run in engine.db.list_runs_for_client(client):
            if run["updated_at"] >= cutoff_text or _already_forgotten(run):
                continue
            if run["status"] not in FINISHED:
                waiting += 1
                continue
            engine.forget_run(run["id"], {"deleted": f"retention: older than {days} days", "at": now.isoformat(timespec="seconds")})
            forgotten += 1
        entry = {
            "client": client, "retention_days": days, "files_deleted": len(files),
            "runs_cleared": forgotten, "runs_past_period_still_waiting": waiting,
        }
        if files or forgotten or waiting:
            engine.db.audit(None, "retention_applied", entry)
        report.append(entry)
    return report


def delete_client(engine: Engine, client: str) -> dict:
    """Remove a client: its folder, its settings, and the payloads of every run
    made for it. Runs still open are cancelled first. This cannot be undone."""
    store = engine.harness.clients
    has_folder = client in store.list_clients()
    scope = store.scope(client) if has_folder else ClientScope(client)
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    runs = engine.db.list_runs_for_client(scope.name)
    if not has_folder and all(_already_forgotten(r) for r in runs):
        raise KeyError(f"unknown client {client!r}")  # never existed, or already deleted
    cancelled = 0
    for run in runs:
        if run["status"] in PENDING_STATUSES or run["status"] in ("queued", "running"):
            engine.db.update_run(run["id"], status="cancelled")
            cancelled += 1
        if not _already_forgotten(run):
            engine.forget_run(run["id"], {"deleted": "client deleted", "at": now})
    files = store.delete_folder(scope) if has_folder else 0  # a folder removed by hand leaves only runs
    engine.db.forget_client_settings(scope.name)
    result = {"client": scope.name, "files_deleted": files, "runs_cleared": len(runs), "runs_cancelled": cancelled}
    engine.db.audit(None, "client_deleted", result)
    return result


def main(argv: list[str] | None = None) -> int:
    from personal_agent.config import load_settings
    from personal_agent.db import Database

    argparse.ArgumentParser(
        prog="python -m personal_agent.retention", description=__doc__.split("\n\n")[0]
    ).parse_args(argv)
    settings = load_settings()
    report = apply_retention(Engine(settings, Database(settings.database_url)))
    if not report:
        print("no client has a retention period set; nothing was removed")
    for r in report:
        print(
            f"{r['client']}: kept {r['retention_days']} days; {r['files_deleted']} files deleted, "
            f"{r['runs_cleared']} runs cleared, {r['runs_past_period_still_waiting']} past the period but still waiting"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
