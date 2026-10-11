# ADR 0007: The weekly learning review

Date: 2026-10-11
Decided by: Builder
Status: Accepted

## What it reads

For the period (seven days by default) and per workflow:

- owner edits: the draft and the version the owner approved (`owner_edits`);
- send-backs and rejections that carry a comment (`approvals`).

Only strategy drafting lets the owner edit before approving, so it is the only
workflow that produces edit evidence today. The others contribute send-back
and rejection comments. A workflow with no evidence gets no model call.

## What it proposes

One strong-tier call per workflow sees that workflow's skill file and the
evidence, and returns up to five proposals. A proposal is a find-and-replace on
the skill file (or an addition at the end), a one-sentence reason, and the run
ids it rests on. Code then discards any proposal that:

- does not apply (the text to find is absent or appears more than once);
- changes nothing;
- cites a run that was not in the evidence;
- names a client folder in the text it adds.

Discarded proposals are listed on the review's run record with the reason.
What survives is stored as `pending` and shown as a diff against the skill
file as it stands.

## What the owner decides

- **Decline:** the row is marked declined. Nothing else happens.
- **Accept:** the change is applied to `skills/<name>/SKILL.md`, the file is
  committed to git on its own (other work in the tree is left alone; nothing
  is pushed), the engine reloads the library and rebuilds its graphs, and the
  audit log records the change with the library version it produced. If the
  file has moved on and the proposal no longer applies, it is marked `stale`
  and nothing is written.

The agent never writes a skill file on any other path.

## Known limits

- **Evidence from several clients meets in one model call.** The review groups
  by workflow, as the PRD says, so drafts from different clients sit in the
  same context. That is not a run for a client, but it is the one place client
  material is pooled. The guard is partial: proposals are told to state general
  rules, any that name a client folder are discarded, and the owner reads every
  diff. A client marked restricted (not built yet) should be excluded from the
  review.
- **A run paused across an accepted change** resumes on the new skill text
  while its run record shows the version it started with.
- **Deployment.** The Docker image copies `skills/` in at build time. An
  accepted change made in the container is not in a git repository and is lost
  when the image is rebuilt; the proposal row records "not committed" and why.
  The server needs `skills/` mounted from a git checkout before the loop is
  used there.
- **Schedule.** Nothing in the app runs weekly by itself. A host cron entry
  runs it, for example
  `0 6 * * 1 docker compose exec -T api python -m personal_agent.learning`.
  n8n is not used for this: the PRD keeps it to the mailbox watcher.
- **Examples.** The review can propose text anywhere in a skill file, including
  the calibration-examples section, but it has no special handling for turning
  an approved piece of work into a worked example.
