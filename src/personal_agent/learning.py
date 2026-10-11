"""The weekly learning review (PRD: control loops / learning; evaluation / weekly report).

The learning loop is the only way the agent improves. Once a week, or on the
owner's request, this job groups what the owner changed, sent back and
rejected by workflow, and proposes changes to that workflow's skill file as
reviewable diffs. The owner accepts or declines each one. An accepted change is
written to the skill file and committed; nothing changes without the owner's
decision, and the agent never edits its own instructions.

The same job produces the weekly report: edit rate, rejection rate, cost and
failed runs by workflow.

    python -m personal_agent.learning            # review the last seven days
"""

from __future__ import annotations

import argparse
import difflib
import json
import re
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from pydantic import BaseModel, Field

from personal_agent.db import Database, new_id
from personal_agent.engine.graph import ROLE
from personal_agent.engine.runner import Engine
from personal_agent.engine.sponsorship_triage import CLIENT as SPONSORSHIP_CLIENT
from personal_agent.harness.budgets import BudgetExceeded, BudgetTracker
from personal_agent.harness.context import build_context
from personal_agent.harness.contract import StepContract, StepFailed
from personal_agent.harness.runner import NodeRunner
from personal_agent.skills.loader import load_library

WORKFLOW = "learning-review"
MAX_EDITS = 20  # most recent owner edits shown per workflow
MAX_COMMENTS = 30
MAX_DIFF_CHARS = 3000


class ProposeIn(BaseModel):
    skill: str
    period: str
    evidence_run_ids: list[str]


class Proposal(BaseModel):
    rationale: str = Field(min_length=1)
    evidence_run_ids: list[str] = Field(min_length=1)
    # Exact text in the skill file to replace; empty means add `replace` at the end.
    find: str = ""
    replace: str


class ProposeOut(BaseModel):
    proposals: list[Proposal] = Field(default=[], max_length=5)


CONTRACT = StepContract(
    node="propose_skill_changes",
    tier="strong",
    limits={"max_tokens": 4000},
    input_schema=ProposeIn,
    output_schema=ProposeOut,
    instruction=(
        "The context holds one workflow's skill file and what the owner did to its output over "
        "the period: text they changed before approving, drafts they sent back, and work they "
        "rejected, each with its run id. Propose changes to the skill file that would have made "
        "those corrections unnecessary. Propose a change only when the same correction shows in "
        "the evidence and a rule, anchor or example in the file would prevent it; one-off edits "
        "are not a pattern. Each proposal gives the exact text to find in the skill file (copied "
        "character for character, long enough to be unique; empty to add at the end), the text "
        "to put in its place, the reason in a sentence, and the run ids it rests on. Write "
        "general rules: never name a client, a person or a figure from the evidence. No "
        "proposals is a valid answer.\n\nOutput schema: "
        + json.dumps(ProposeOut.model_json_schema(), sort_keys=True)
    ),
)


# --- evidence and the weekly report (code) -------------------------------------------
def _diff(before: str, after: str) -> str:
    lines = difflib.unified_diff(before.splitlines(), after.splitlines(), "draft", "approved", lineterm="", n=1)
    return "\n".join(lines)[:MAX_DIFF_CHARS]


def gather_evidence(db: Database, since: str, until: str) -> dict[str, dict]:
    """{workflow: {"edits": [...], "comments": [...]}} for the period."""
    evidence: dict[str, dict] = {}

    def bucket(workflow: str) -> dict:
        return evidence.setdefault(workflow, {"edits": [], "comments": []})

    for edit in db.owner_edits_between(since, until):
        bucket(edit["workflow"])["edits"].append({
            "run_id": edit["run_id"], "stage": edit["stage"], "diff": _diff(edit["before"], edit["after"]),
        })
    for decision in db.approvals_between(since, until):
        if decision["decision"] in ("sent_back", "rejected") and (decision["comment"] or "").strip():
            bucket(decision["workflow"])["comments"].append({
                "run_id": decision["run_id"], "decision": decision["decision"], "comment": decision["comment"],
            })
    for found in evidence.values():
        found["edits"], found["comments"] = found["edits"][-MAX_EDITS:], found["comments"][-MAX_COMMENTS:]
    return evidence


def weekly_report(db: Database, since: str, until: str) -> dict[str, dict]:
    """Edit rate, rejection rate, cost and failed runs by workflow (PRD: evaluation)."""
    report: dict[str, dict] = {}

    def row(workflow: str) -> dict:
        return report.setdefault(workflow, {
            "runs": 0, "failed_runs": 0, "cost_usd": 0.0,
            "approved": 0, "edited": 0, "sent_back": 0, "rejected": 0,
        })

    for run in db.runs_between(since, until):
        if run["workflow"] == WORKFLOW:
            continue
        r = row(run["workflow"])
        r["runs"] += 1
        r["failed_runs"] += run["status"] == "failed"
        r["cost_usd"] = round(r["cost_usd"] + (run["cost_usd"] or 0.0), 4)
    for decision in db.approvals_between(since, until):
        row(decision["workflow"])[decision["decision"]] += 1
    for edit in db.owner_edits_between(since, until):
        row(edit["workflow"])["edited"] += 1
    for r in report.values():
        decisions = r["approved"] + r["sent_back"] + r["rejected"]
        # Share of approvals the owner changed first, and share of decisions that were rejections.
        r["edit_rate"] = round(r["edited"] / r["approved"], 3) if r["approved"] else None
        r["rejection_rate"] = round(r["rejected"] / decisions, 3) if decisions else None
    return report


# --- proposals (code) ------------------------------------------------------------------
def apply_change(text: str, find: str, replace: str) -> str:
    """The skill text with one proposal applied; ValueError if it does not apply cleanly."""
    if not find:
        return text.rstrip("\n") + "\n\n" + replace.strip("\n") + "\n"
    if text.count(find) != 1:
        raise ValueError(
            "the text to replace appears " + ("nowhere" if find not in text else "more than once")
            + " in the skill file"
        )
    return text.replace(find, replace)


def skill_diff(text: str, find: str, replace: str) -> str:
    changed = apply_change(text, find, replace)
    return "\n".join(difflib.unified_diff(
        text.splitlines(), changed.splitlines(), "SKILL.md (current)", "SKILL.md (proposed)", lineterm=""
    ))


def _invalid(proposal: dict, skill_text: str, known_runs: set[str], client_names: list[str]) -> str | None:
    """Why a proposal cannot be shown to the owner, or None."""
    try:
        if apply_change(skill_text, proposal["find"], proposal["replace"]) == skill_text:
            return "it changes nothing"
    except ValueError as e:
        return str(e)
    if not set(proposal["evidence_run_ids"]) <= known_runs:
        return "it cites a run that is not in the evidence"
    for name in client_names:
        if re.search(rf"\b{re.escape(name)}\b", proposal["replace"], re.IGNORECASE):
            return f"it names a client ({name}); a skill holds general rules, not client material"
    return None


def _skill_path(skills_dir: Path, skill: str) -> Path:
    return skills_dir / skill / "SKILL.md"


# --- the review ------------------------------------------------------------------------
def run_review(engine: Engine, days: int = 7, now: datetime | None = None, by_hand: bool = False) -> dict:
    """One learning review over the last `days`; returns its run record. The
    scheduled review is refused at the monthly cap; one the owner starts is not."""
    db, settings = engine.db, engine.settings
    engine.check_monthly_cap(by_hand, "the learning review")
    now = now or datetime.now(timezone.utc) + timedelta(seconds=1)  # include decisions made this second
    until = now.isoformat(timespec="seconds")
    since = (now - timedelta(days=days)).isoformat(timespec="seconds")
    version, skills = load_library(settings.skills_dir)
    run_id = new_id()
    db.create_run(run_id, WORKFLOW, "all clients", {"since": since, "until": until}, version)
    db.update_run(run_id, status="running")
    db.audit(run_id, "learning_review_started", {"since": since, "until": until})

    # Client folder names, so a proposal that carries one into a shared skill is discarded.
    client_names = [c for c in engine.harness.clients.list_clients() if len(c) >= 3 and c != SPONSORSHIP_CLIENT]
    tracker = BudgetTracker(settings.budgets)
    evidence = gather_evidence(db, since, until)
    proposed: list[str] = []
    discarded: list[dict] = []
    skipped: list[str] = []
    status, reason = "done", None
    try:
        for workflow, found in sorted(evidence.items()):
            if workflow not in skills:
                skipped.append(f"{workflow}: no skill file")
                continue
            run_ids = sorted({e["run_id"] for e in found["edits"] + found["comments"]})
            # Drafts derive from client material, so the evidence travels as marked data.
            chunks = [(f"owner edit, run {e['run_id']}, {e['stage'] or 'final'}", e["diff"]) for e in found["edits"]]
            chunks += [
                (f"owner {c['decision'].replace('_', ' ')}, run {c['run_id']}", c["comment"]) for c in found["comments"]
            ]
            payload = {"skill": workflow, "period": f"{since} to {until}", "evidence_run_ids": run_ids}
            context, _ = build_context(ROLE, skills[workflow], [], chunks, payload)
            out = engine.harness.node_runner(tracker, run_id).run(CONTRACT, payload, context=context)
            for proposal in out["proposals"]:
                why = _invalid(proposal, skills[workflow], set(run_ids), client_names)
                if why:
                    discarded.append({"skill": workflow, "rationale": proposal["rationale"], "reason": why})
                    continue
                proposed.append(db.create_proposal(
                    run_id, workflow, proposal["rationale"], proposal["evidence_run_ids"],
                    proposal["find"], proposal["replace"],
                ))
    except (StepFailed, BudgetExceeded) as e:
        status, reason = "failed", str(e)
    except Exception as e:  # provider or unexpected errors: stop with a clear reason, keep what was proposed
        status, reason = "failed", f"{type(e).__name__}: {e}"

    db.update_run(
        run_id, status=status, error_reason=reason,
        outputs={
            "period": {"since": since, "until": until},
            "report": weekly_report(db, since, until),
            "workflows_reviewed": sorted(w for w in evidence if w in skills),
            "proposals": proposed,
            "discarded": discarded,
            "skipped": skipped,
        },
        tokens_in=tracker.tokens_in, tokens_out=tracker.tokens_out, cost_usd=tracker.cost_usd,
    )
    db.audit(run_id, "learning_review_finished", {"proposals": len(proposed), "discarded": len(discarded)})
    return db.get_run(run_id)


# --- the owner's decision ----------------------------------------------------------------
def _commit(path: Path, message: str) -> tuple[str | None, str | None]:
    """(commit id, None) if the skill file sits in a git work tree and the commit
    succeeds; otherwise (None, why not). Commits that one file only; never pushes."""
    def git(*args: str) -> subprocess.CompletedProcess:
        return subprocess.run(["git", "-C", str(path.parent), *args], capture_output=True, text=True, timeout=60)

    try:
        if git("rev-parse", "--is-inside-work-tree").stdout.strip() != "true":
            return None, "the skill library is not in a git repository"
        git("add", "--", path.name)
        done = git("commit", "-m", message, "--", path.name)
        if done.returncode != 0:
            return None, (done.stderr or done.stdout).strip()[:300]
        return git("rev-parse", "--short", "HEAD").stdout.strip(), None
    except (OSError, subprocess.SubprocessError) as e:
        return None, f"git could not be run: {e}"


def decide(engine: Engine, proposal_id: str, decision: str, note: str | None = None) -> dict:
    """The owner accepts or declines one proposed change. Accepting writes the
    skill file, commits it and reloads the library; declining changes nothing."""
    db = engine.db
    proposal = db.get_proposal(proposal_id)
    if not proposal:
        raise KeyError(proposal_id)
    if proposal["status"] != "pending":
        raise ValueError(f"proposal {proposal_id} is already {proposal['status']}")
    if decision == "declined":
        db.decide_proposal(proposal_id, "declined", decision_note=note)
        db.audit(proposal["review_run_id"], "skill_change_declined", {"proposal": proposal_id, "note": note})
        return db.get_proposal(proposal_id)
    if decision != "accepted":
        raise ValueError("decision must be accepted or declined")

    path = _skill_path(engine.settings.skills_dir, proposal["skill"])
    try:
        changed = apply_change(path.read_text(encoding="utf-8"), proposal["find"], proposal["replace"])
    except (OSError, ValueError) as e:
        # The file moved on since the proposal was written: it is set aside, not forced.
        db.decide_proposal(proposal_id, "stale", decision_note=str(e))
        raise ValueError(f"the proposal no longer applies: {e}") from e
    path.write_text(changed, encoding="utf-8", newline="\n")
    commit, why_not = _commit(
        path, f"Skill library: {proposal['skill']}: {proposal['rationale'][:72]}\n\n"
              f"Accepted by the owner (learning review proposal {proposal_id})."
    )
    version = engine.reload_library()
    db.decide_proposal(
        proposal_id, "accepted", decision_note=note or why_not, commit=commit, library_version=version
    )
    # A configuration change: the audit log records it with the version it produced.
    db.audit(proposal["review_run_id"], "skill_change_accepted", {
        "proposal": proposal_id, "skill": proposal["skill"], "commit": commit,
        "not_committed": why_not, "library_version": version,
    })
    return db.get_proposal(proposal_id)


def main(argv: list[str] | None = None) -> int:
    from personal_agent.config import load_settings

    parser = argparse.ArgumentParser(prog="python -m personal_agent.learning", description=__doc__.split("\n\n")[0])
    parser.add_argument("--days", type=int, default=7, help="how far back the review looks")
    args = parser.parse_args(argv)
    from personal_agent.costs import MonthlyCapReached

    settings = load_settings()
    try:
        run = run_review(Engine(settings, Database(settings.database_url)), days=args.days)
    except MonthlyCapReached as e:
        print(f"learning review not started: {e}")
        return 1
    out = run["outputs"]
    print(f"learning review {run['id']}: {run['status']}" + (f" ({run['error_reason']})" if run["error_reason"] else ""))
    for workflow, r in sorted(out["report"].items()):
        print(
            f"  {workflow:<20} runs {r['runs']}, failed {r['failed_runs']}, cost ${r['cost_usd']:.2f}, "
            f"edit rate {r['edit_rate']}, rejection rate {r['rejection_rate']}"
        )
    print(f"  {len(out['proposals'])} proposals waiting for the owner, {len(out['discarded'])} discarded")
    return 0 if run["status"] == "done" else 1


if __name__ == "__main__":
    sys.exit(main())
