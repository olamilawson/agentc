# Brief-evaluation rubric worksheet — for the owner to fill in

The PRD is explicit about what a rubric must contain and who writes it:
**the owner writes the rubric; the builder helps test it.** Nothing below is
final — the draft criteria are a starting point to edit, strike out or replace.

## What the rubric is

The scoring standard the agent applies to every incoming brief. When the agent
scores a brief, it must rate each criterion from **1 to 5** and **quote the
exact lines of the brief** that justify the score. Before the workflow goes
live, the agent's scores on your fifteen real past briefs must agree with your
own scores (within one point, at a level you set) — that agreement is the
release gate. So the rubric below becomes the single source of truth both you
and the agent are measured against.

## What each criterion needs (PRD: evaluation)

1. **Definition** — what this criterion measures, in one sentence.
2. **Anchors** — what a 1 looks like, what a 3 looks like, what a 5 looks like.
3. **Evidence rule** — what counts as proof in the brief text.

## Draft criteria — edit freely

### 1. Objective clarity

- Definition: Is the business objective specific, singular and measurable?
- 1: No objective stated, or a vague ambition ("grow the brand").
- 3: An objective is stated but fuzzy or contains multiple competing goals.
- 5: One clear objective with a number or deadline attached.
- Evidence rule: quote the sentence stating the objective.

### 2. Audience definition

- Definition: Does the brief say who the work is for, specifically?
- 1: No audience mentioned.
- 3: A broad segment named without differentiating detail.
- 5: A named audience with a useful distinguishing characteristic.
- Evidence rule: quote the audience description.

### 3. Budget realism

- Definition: Is the stated budget sufficient and proportionate to the ask?
- 1: No budget, or one that cannot plausibly deliver the deliverables.
- 3: Budget present but thin; trade-offs not acknowledged.
- 5: Budget with enough detail to judge the trade-offs.
- Evidence rule: quote the budget line (or note its absence verbatim).

### 4. Timeline feasibility

- Definition: Can the deliverables realistically be produced in the stated window?
- 1: No dates, or a window that makes the deliverables impossible.
- 3: Dates present but tight; dependencies unexamined.
- 5: Dates with milestones and dependencies acknowledged.
- Evidence rule: quote the timeline.

### 5. Deliverable clarity

- Definition: Is it clear what artefacts are expected at the end?
- 1: Deliverables unstated or unbounded ("whatever it takes").
- 3: Deliverables listed but loosely defined.
- 5: Named deliverables with formats and quantities.
- Evidence rule: quote the deliverables list.

### 6. Success measures

- Definition: Does the brief say how success will be judged afterwards?
- 1: Nothing stated.
- 3: A metric named but unattached to a target.
- 5: Metric with a target and a measurement moment.
- Evidence rule: quote the success measure.

### 7. Fit

- Definition: Does this brief fit the owner's positioning and current priorities?
- 1: Outside the owner's areas of work.
- 3: Adjacent, but the connection is unproven.
- 5: Clearly within an active area, with context to do it well.
- Evidence rule: name the connection (this may cite your workstream context
  rather than the brief text itself).

## Your decisions for the fifteen items

For each real past brief, the evaluation set needs (PRD: evaluation):

- the brief itself (text, or a file we place in the client folder),
- **your own score per criterion** (or an overall score, if you prefer — say which),
- **what you did**: replied with questions, pursued it, or declined it,
- optionally, what you would have changed in any reply you sent.

A template line per item:

```
item-01: [brief attached / pasted below]
  scores:  objective=4 audience=3 budget=? timeline=? deliverables=? success=? fit=?
  decision:  asked-for-detail | pursued | declined
  note:      what a good reply needed, in one line
```

Fifteen items is the PRD minimum. If client contracts do not allow real briefs
to be used, substitute similar briefs you have written yourself.

## How to hand this back

Edit this file directly and send it back, or reply in chat in any shape — the
builder turns the final version into `skills/brief-evaluation/SKILL.md` and the
fifteen items into `evaluation/sets/brief-evaluation.jsonl`, then builds the
brief-evaluation graph against them (the PRD's rule: the evaluation set is
written before the graph it tests).
