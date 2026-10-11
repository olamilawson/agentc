# ADR 0006: Strategy drafting — the owner's pauses, edits, and what the harness gained for them

Date: 2026-10-11
Decided by: Builder
Status: Accepted; the templates and writing standard are provisional

## Three ways to answer a pause

The PRD has the owner approve, edit then approve, send back with a comment, or
reject. Until now only approve and reject did anything. For this graph:

- **Edit then approve.** An item marked editable can be approved with an edited
  body. The approval is recorded against the hash of the owner's version, the
  graph accepts it only if that hash matches, and the save step writes exactly
  that version. The draft and the owner's version are stored in `owner_edits`
  for the learning loop. An edited outline is read back one line per section.
- **Send back.** A workflow marked `revises` resumes with the owner's comment.
  At the outline it is redone with the comment; at the end the comment becomes
  a fault on every section and a new critique-and-revise pass runs. Other
  workflows still only record a send-back.
- **Questions.** The run stops with status `waiting_for_input` and resumes with
  the owner's answers (`POST /runs/{id}/input`). It asks once.

Editing is offered only where the content is a document for the owner to file.
The sponsorship reply and the other graphs are unchanged: their queue items
are not editable.

## The critique loop

Critique runs as a separate model call on the finished draft, after a code
check that every figure of two or more digits in a section appears in the goal,
the owner's answers or the sources. Faults are fixed section by section; only
faulted sections are rewritten. Up to three critiques and two revisions run;
faults still open go to the owner beside the document, not inside it.

The figure check is deliberately blunt. It will flag a sound derived number (a
total, a percentage worked out from the sources) and a year the sources do not
mention. Those reach the owner as open faults rather than passing silently.

## Sources

Sources are files in the run's client folder, named in the request or, if none
are named, the first ten files in the folder. They are read through the gateway
(so the client scope check applies), sent as untrusted data, and shortened to
24,000 characters in total; a shortened source is flagged on the run. There is
no retrieval by relevance yet, so a long folder is read from the top, not
searched.

## One section per graph step

Each section is its own step so a finished section is checkpointed and a
failure keeps the earlier ones. The engine's step limit per invocation is
raised to 80; every loop is still bounded in the graph (sections by the
outline, revisions by the limit, owner passes by the owner).

## No evaluation set

The working rule is to write the evaluation set before the graph. This graph
has none: the PRD's measures are the owner's edit rate and faults the critic
missed, which need documents the owner has edited. The graph records what
those measures need; the set and a report over the recorded edits are owed.

## Provisional

`skills/strategy-drafting/SKILL.md` (three templates, required information per
type, the writing standard, the critique checklist) is builder-written from the
Digital Twin Agent Scope and awaits the owner.
