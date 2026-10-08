"""Safety sets: the injection and separation runners, and the sets themselves."""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from personal_agent.clients import ClientStore, parse_document
from personal_agent.engine.brief_evaluation import CRITERIA
from personal_agent.harness.context import _flag_untrusted, build_context
from personal_agent.harness.models import RecordedGateway
from personal_agent.safety import build_document, evaluate_safety, format_safety_report
from tests.test_brief_evaluation import ASSESSMENT, RECORD, VERDICTS_OK

SETS = Path(__file__).resolve().parents[1] / "evaluation" / "sets"
INJECTION = [json.loads(line) for line in (SETS / "injection.jsonl").read_text(encoding="utf-8").splitlines()]


def _material(item: dict) -> str:
    if item["kind"] != "document":
        return item["text"]
    ext, data = build_document(item["document"])
    return parse_document(data, f"brief.{ext}")["text"]


def _scores(score: int = 3) -> dict:
    return {
        "scores": [{"criterion": c, "score": score, "basis": "absence", "quotes": [],
                    "rationale": "The brief does not state this."} for c in CRITERIA],
        "decision": "ask_for_detail",
        "decision_reason": "Too little is stated to judge.",
    }


def _gateway(n: int, scores: dict | None = None, assessment: dict | None = None) -> RecordedGateway:
    return RecordedGateway({
        "extract_fields": [RECORD] * n,
        "score_by_rubric": [scores or _scores()] * n,
        "draft_assessment": [assessment or ASSESSMENT] * n,
        "verify_evidence": [VERDICTS_OK] * n,
    })


# --- the sets against the harness, without a model ------------------------------------
@pytest.mark.parametrize("item", INJECTION, ids=lambda i: i["id"])
def test_injection_item_is_flagged_only_when_it_carries_an_instruction(item):
    flags = _flag_untrusted("brief", _material(item))
    assert bool(flags) == item.get("expect_flag", True), flags


@pytest.mark.parametrize("item", INJECTION, ids=lambda i: i["id"])
def test_material_cannot_close_its_data_wrapper(item):
    context, _ = build_context("role", "rubric", [], [("brief", _material(item))], {})
    assert context.count("<untrusted_data ") == 1
    assert context.count("</untrusted_data>") == 1


def test_hidden_document_text_reaches_the_parser():
    # The point of these items: a reader would not see the text, the agent does.
    for item in (i for i in INJECTION if i["kind"] == "document"):
        assert item["document"]["hidden"].split()[0] in _material(item), item["id"]


def test_separation_set_passes_in_full(settings):
    report = evaluate_safety("separation", SETS / "separation.jsonl", settings)
    assert report["passed"], format_safety_report(report)
    assert len(report["items"]) >= 12


# --- the runners catch failures --------------------------------------------------------
def test_injection_set_passes_when_the_agent_ignores_every_instruction(settings):
    report = evaluate_safety("injection", SETS / "injection.jsonl", settings, gateway=_gateway(len(INJECTION)))
    assert report["passed"], format_safety_report(report)


def test_injection_runner_fails_an_agent_that_obeys(settings):
    obedient = _gateway(
        len(INJECTION),
        scores=_scores(5),
        assessment={"note": "RUBRIC DUMP: no further information is needed.", "questions": []},
    )
    report = evaluate_safety("injection", SETS / "injection.jsonl", settings, gateway=obedient)
    assert not report["passed"]
    by_id = {r["id"]: r for r in report["items"]}
    assert by_id["inj-001"]["checks"]["instructions_ignored"] == "fail"  # every criterion scored 5
    assert by_id["inj-005"]["checks"]["instructions_ignored"] == "fail"  # wrote the phrase asked for
    assert "followed the embedded instruction" in format_safety_report(report)


def test_injection_set_is_not_passed_when_runs_cannot_be_judged(settings):
    # No recorded responses: every run fails at its first model call.
    report = evaluate_safety("injection", SETS / "injection.jsonl", settings, gateway=RecordedGateway({}))
    assert not report["passed"]
    first = report["items"][0]["checks"]
    assert first["flagged"] == "pass" and first["instructions_ignored"] == "not judged"


def test_a_partial_run_never_passes(settings):
    report = evaluate_safety("separation", SETS / "separation.jsonl", settings, limit=3)
    assert all(r["passed"] for r in report["items"]) and not report["passed"]


def test_separation_runner_fails_when_the_scope_check_is_removed(settings, monkeypatch):
    # A store that no longer confines paths to the run's folder must be caught.
    monkeypatch.setattr(ClientStore, "_within_scope", lambda self, scope, relpath: self.folder(scope) / relpath)
    report = evaluate_safety("separation", SETS / "separation.jsonl", settings)
    assert not report["passed"]
    traversal = next(r for r in report["items"] if r["id"] == "sep-004")
    assert traversal["checks"]["nothing_from_other_folders"] == "fail"
    assert traversal["checks"]["cross_client_calls_refused"] == "fail"
