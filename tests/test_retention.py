"""Per-client retention and deletion: what is removed, what stays, and what is refused."""

from __future__ import annotations

import os
import time
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from personal_agent import costs, retention, tracing
from personal_agent.api.main import create_app
from personal_agent.config import SponsorshipPolicy
from personal_agent.db import Database, audit_log
from personal_agent.engine.runner import Engine
from personal_agent.harness.models import RecordedGateway
from tests.test_brief_evaluation import ASSESSMENT, BRIEF, RECORD, SCORES, VERDICTS_OK
from tests.test_sponsorship_triage import CHECK_OK, ENQUIRY, MAIL, REPLY, scored

SECRET = "150,000 activations"  # a phrase from the brief that ends up in scores, traces and files


def _engine(settings, n: int = 2) -> Engine:
    settings.sponsorship = SponsorshipPolicy(pursue_at=4.0, decline_below=2.5, escalate_above=10_000_000)
    gateway = RecordedGateway({
        "extract_fields": [RECORD] * n, "score_by_rubric": [SCORES] * n,
        "draft_assessment": [ASSESSMENT] * n, "verify_evidence": [VERDICTS_OK] * n,
        "classify": [ENQUIRY], "score_and_route": [scored(5)], "draft_reply": [REPLY], "check_reply": [CHECK_OK],
    })
    return Engine(settings, Database(settings.database_url), gateway=gateway)


def _brief_run(engine: Engine, client: str, approve: bool = True) -> str:
    scope = engine.harness.clients.ensure(client)
    engine.harness.clients.store_file(scope, "brief.md", BRIEF.encode())
    run_id = engine.start_run("brief-evaluation", client, {"brief_path": "brief.md"})
    if approve:
        engine.approve(run_id, "approved", None)
    return run_id


def _everything_stored_about(engine: Engine, run_id: str) -> str:
    """Every place a run's material is kept, as one string to search."""
    trace = tracing.build_trace(engine.db, run_id)
    snap = engine.graphs["brief-evaluation"].get_state({"configurable": {"thread_id": run_id}})
    return repr([trace, snap.values, engine.db.list_owner_edits(run_id)])


def test_deleting_a_client_removes_its_folder_and_every_copy_of_its_material(settings):
    engine = _engine(settings)
    acme_run, beta_run = _brief_run(engine, "acme"), _brief_run(engine, "beta")
    assert SECRET in _everything_stored_about(engine, acme_run)
    cost_before = costs.summary(engine.db, settings)

    result = retention.delete_client(engine, "acme")
    assert result == {"client": "acme", "files_deleted": 2, "runs_cleared": 1, "runs_cancelled": 0}
    assert not (settings.clients_dir / "acme").exists()
    assert SECRET not in _everything_stored_about(engine, acme_run)

    run = engine.db.get_run(acme_run)
    assert run["inputs"] is None and run["outputs"]["deleted"] == "client deleted"
    # The run itself, its steps and its cost remain, so history still adds up.
    assert (run["status"], run["client"], run["cost_usd"]) == ("done", "acme", 0.008)
    assert len(tracing.build_trace(engine.db, acme_run)["steps"]) > 5
    assert costs.summary(engine.db, settings)["total_usd"] == cost_before["total_usd"]

    # The other client is untouched.
    assert SECRET in _everything_stored_about(engine, beta_run)
    assert (settings.clients_dir / "beta" / "brief.md").exists()

    with engine.db.engine.connect() as cx:
        audits = [dict(r) for r in cx.execute(select(audit_log)).mappings()]
    assert any(a["action"] == "client_deleted" and a["detail"]["client"] == "acme" for a in audits)
    assert any(a["action"] == "run_created" and a["run_id"] == acme_run for a in audits)  # the log is not erased


def test_deleting_a_client_cancels_its_open_runs_and_empties_the_queue(settings):
    engine = _engine(settings, n=1)
    run_id = _brief_run(engine, "acme", approve=False)
    assert len(engine.queue()) == 1
    assert retention.delete_client(engine, "acme")["runs_cancelled"] == 1
    assert engine.queue() == [] and engine.db.get_run(run_id)["status"] == "cancelled"
    with pytest.raises(ValueError):
        engine.approve(run_id, "approved", None)
    with pytest.raises(KeyError):
        retention.delete_client(engine, "acme")  # nothing is left to delete


def test_deleting_the_sponsorship_client_removes_ledger_rows_and_held_mail(settings):
    engine = _engine(settings)
    run_id = engine.start_run("sponsorship-triage", "sponsorship", MAIL)
    engine.approve(run_id, "approved", None)
    assert len(engine.db.list_ledger()) == 1 and len(engine.db.list_outbox()) == 1
    retention.delete_client(engine, "sponsorship")
    assert engine.db.list_ledger() == [] and engine.db.list_outbox() == []
    assert MAIL["sender"] not in repr(tracing.build_trace(engine.db, run_id))


def test_retention_removes_only_what_is_older_than_the_clients_period(settings):
    engine = _engine(settings)
    old_run = _brief_run(engine, "acme")
    kept_forever = _brief_run(engine, "beta")  # beta has no period set
    store, acme = engine.harness.clients, engine.harness.clients.scope("acme")
    store.store_file(acme, "old.md", b"last year's plan")
    store.store_file(acme, "new.md", b"this week's plan")
    long_ago = time.time() - 40 * 86400
    os.utime(settings.clients_dir / "acme" / "old.md", (long_ago, long_ago))

    retention.set_retention(engine, "acme", 30)
    assert retention.apply_retention(engine) == [{
        "client": "acme", "retention_days": 30, "files_deleted": 1,
        "runs_cleared": 0, "runs_past_period_still_waiting": 0,
    }]
    assert sorted(f["path"] for f in store.list_files(acme))[-2:] == ["brief.md", "new.md"]
    assert SECRET in _everything_stored_about(engine, old_run)  # the run is recent: kept

    # Forty days on, the run and the remaining files are past the period.
    later = datetime.now(timezone.utc) + timedelta(days=40)
    (report,) = retention.apply_retention(engine, now=later)
    assert (report["files_deleted"], report["runs_cleared"]) == (3, 1)
    assert SECRET not in _everything_stored_about(engine, old_run)
    assert engine.db.get_run(old_run)["outputs"]["deleted"] == "retention: older than 30 days"
    assert SECRET in _everything_stored_about(engine, kept_forever)
    assert (settings.clients_dir / "acme").is_dir()  # the client itself remains
    assert retention.apply_retention(engine, now=later)[0]["runs_cleared"] == 0  # nothing is cleared twice


def test_retention_never_removes_a_run_still_waiting_for_the_owner(settings):
    engine = _engine(settings, n=1)
    run_id = _brief_run(engine, "acme", approve=False)
    retention.set_retention(engine, "acme", 7)
    (report,) = retention.apply_retention(engine, now=datetime.now(timezone.utc) + timedelta(days=30))
    assert (report["runs_cleared"], report["runs_past_period_still_waiting"]) == (0, 1)
    assert engine.approve(run_id, "approved", None)["status"] == "done"  # still approvable


def test_retention_period_rules(settings):
    engine = _engine(settings)
    engine.harness.clients.ensure("acme")
    with pytest.raises(ValueError):
        retention.set_retention(engine, "acme", 0)
    with pytest.raises(KeyError):
        retention.set_retention(engine, "nobody", 30)
    retention.set_retention(engine, "acme", 30)
    assert retention.set_retention(engine, "acme", None) == {"client": "acme", "retention_days": None}
    assert retention.apply_retention(engine) == []  # no period, nothing considered


def test_api_and_page_set_retention_and_require_the_name_to_delete(settings):
    client = TestClient(create_app(settings, gateway=RecordedGateway({})))
    client.post("/clients", json={"name": "acme"})
    client.post("/clients/acme/files", files={"file": ("brief.md", BRIEF.encode())})

    assert client.post("/clients/acme/retention", data={"days": "90"}).json()["retention_days"] == 90
    assert client.get("/clients/acme").json() == {
        "client": "acme", "files": 1, "bytes": len(BRIEF), "runs": 0, "retention_days": 90, "restricted": False,
    }
    assert client.post("/clients/acme/retention", data={"days": ""}).json()["retention_days"] is None
    assert client.post("/clients/acme/retention", json={"days": 0}).status_code == 400
    assert client.post("/clients/nobody/retention", json={"days": 5}).status_code == 404
    page = client.get("/files").text
    assert "Delete this client" in page and "1 files" in page and 'href="/files"' in page

    assert client.post("/clients/acme/delete", data={"confirm": "Acme"}).status_code == 400
    assert client.post("/clients/acme/delete").status_code == 400
    assert client.get("/clients").json() == ["acme"]  # a wrong confirmation deletes nothing
    assert client.post("/clients/acme/delete", data={"confirm": "acme"}).json()["files_deleted"] == 1
    assert client.get("/clients").json() == []
    assert client.post("/clients/acme/delete", data={"confirm": "acme"}).status_code == 404
    assert client.post("/retention/apply").json() == []
