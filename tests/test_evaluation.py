"""Evaluation runner: agreement against the reference scores and the release gate."""

from __future__ import annotations

import copy
import json

from personal_agent.db import Database
from personal_agent.engine.brief_evaluation import CRITERIA
from personal_agent.evaluation import evaluate, format_report
from personal_agent.harness.models import RecordedGateway
from tests.test_brief_evaluation import ASSESSMENT, BRIEF, INVENTED, RECORD, SCORES, VERDICTS_OK

AGENT = {c: 4 for c in CRITERIA}  # what SCORES gives every criterion


def _item(n: int, scores: dict, decision: str = "pursue", owner: bool = True, **extra) -> dict:
    scored = {"scores": scores, "decision": decision, **extra}
    return {
        "id": f"brief-{n:03d}",
        "synthetic": not owner,
        "brief": BRIEF,
        "provisional": None if owner else scored,
        "owner": scored if owner else None,
    }


def _run(settings, tmp_path, items, score_responses=None, **kwargs) -> dict:
    set_path = tmp_path / "set.jsonl"
    set_path.write_text("\n".join(json.dumps(i) for i in items), encoding="utf-8")
    n = kwargs.get("limit") or len(items)
    score_responses = score_responses or [SCORES] * n
    gateway = RecordedGateway({
        "extract_fields": [RECORD] * n,
        "score_by_rubric": score_responses,
        "draft_assessment": [ASSESSMENT] * len(score_responses),
        "verify_evidence": [VERDICTS_OK] * n,
    })
    return evaluate("brief-evaluation", set_path, settings, gateway=gateway, **kwargs)


def test_owner_scored_set_passes_the_gate_at_the_owners_level(settings, tmp_path):
    items = [_item(n, {**AGENT, "audience": 5}) for n in range(15)]  # one point apart on one criterion
    report = _run(settings, tmp_path, items, min_agreement=0.9)
    s = report["summary"]
    assert (s["items"], s["failed"], s["invalid_quotes"]) == (15, 0, 0)
    assert s["within_one"] == 1.0 and s["exact"] == 0.875
    assert s["by_criterion"]["audience"]["mean_diff"] == -1.0
    assert s["decision_agreement"] == 1.0
    assert report["gate"] == {"passed": True, "min_agreement": 0.9, "blocked_by": []}
    assert "RELEASE GATE: passed" in format_report(report)


def test_agreement_below_the_owners_level_blocks_the_gate(settings, tmp_path):
    items = [_item(n, {**AGENT, "audience": 1, "measurability": 2}, decision="decline") for n in range(15)]
    report = _run(settings, tmp_path, items, min_agreement=0.8)
    assert report["summary"]["within_one"] == 0.75
    assert report["summary"]["decision_agreement"] == 0.0
    assert not report["gate"]["passed"]
    assert any("below the owner's level" in r for r in report["gate"]["blocked_by"])
    assert "audience +3" in format_report(report)


def test_synthetic_set_never_satisfies_the_gate(settings, tmp_path):
    items = [_item(n, AGENT, owner=False) for n in range(15)]
    report = _run(settings, tmp_path, items, min_agreement=0.5)
    assert report["summary"]["within_one"] == 1.0  # measured against the provisional scores
    assert not report["gate"]["passed"]
    assert any("synthetic" in r for r in report["gate"]["blocked_by"])


def test_gate_needs_the_owners_level_the_whole_set_and_fifteen_items(settings, tmp_path):
    report = _run(settings, tmp_path, [_item(n, AGENT) for n in range(3)], limit=2)
    blocked = " | ".join(report["gate"]["blocked_by"])
    assert "only 2 of the set's 3 items" in blocked
    assert "minimum is 15" in blocked
    assert "has not set an agreement level" in blocked


def test_invalid_quotes_and_failed_runs_block_the_gate(settings, tmp_path):
    items = [_item(n, AGENT) for n in range(15)]
    # Item 0 invents a quote on all three scoring passes; the last item has no recording left.
    report = _run(settings, tmp_path, items, score_responses=[INVENTED] * 3 + [SCORES] * 13, min_agreement=0.5)
    s = report["summary"]
    assert s["invalid_quotes"] == 1 and s["items_with_open_faults"] == 1
    assert s["failed"] == 1
    assert report["items"][-1]["completed"] is False
    blocked = " | ".join(report["gate"]["blocked_by"])
    assert "quotes do not appear" in blocked and "runs failed" in blocked


def test_missed_gaps_are_counted_where_the_reference_lists_them(settings, tmp_path):
    # The graph finds client and constraints missing from RECORD; "budget" it does not.
    items = [_item(0, AGENT, gaps=["client", "budget"]), _item(1, AGENT)]
    report = _run(settings, tmp_path, items)
    assert report["items"][0]["missed_gaps"] == ["budget"]
    assert report["items"][1]["missed_gaps"] is None
    assert report["summary"]["missed_gaps"] == 1


def test_evaluation_leaves_the_real_run_record_and_folders_untouched(settings, tmp_path):
    real = Database(settings.database_url)
    _run(settings, tmp_path, [_item(0, AGENT)])
    assert real.list_runs() == []
    assert not settings.clients_dir.exists()
