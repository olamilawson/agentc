---
name: sponsorship-triage
description: Criteria, risk flags, tone rules and reply templates for sponsorship enquiries. PROVISIONAL — derived by the builder from the Digital Twin Agent Scope (umbrellas 3, 16, 17, 20 and the Core Judgement Layer) on 2026-10-09. The owner has not yet set the criteria; replace this file with the owner's own before the workflow goes live.
---

# Sponsorship triage

The thresholds that turn scores into a route live in `config/sponsorship.yaml`,
not here. This file says how to score an enquiry and how the reply should read.

## Is it a sponsorship request?

Yes when someone asks the company to fund, partner on, or lend its name to an
event, programme, team, publication or cause. No for supplier pitches, job
applications, media sales, customer complaints, newsletters and automated mail.

## Criteria

Rate each from 1 to 5 on what the enquiry states. Score what is present; list
what is missing rather than guessing.

### strategic_fit

- Source: umbrella 20 (evaluate strategic fit); Core Judgement Layer, "Is it
  strategically important?"
- 1: No connection to the company's business, audiences or current priorities.
- 3: An adjacent audience or theme; the connection is asserted, not shown.
- 5: A clear line from the opportunity to a current business priority.

### audience_relevance

- Source: Core Judgement Layer, "Does anyone care? Is it appropriate for the
  audience?"
- 1: No audience stated, or one the company does not serve.
- 3: A broad audience with some overlap.
- 5: A named, sized audience the company wants to reach.

### commercial_value

- Source: umbrella 17 (spend has a defensible business rationale; sponsorship
  evaluation); "Is this commercially sensible?"
- 1: No stated benefits, or an ask out of proportion to them.
- 3: Standard visibility benefits; return unexamined.
- 5: Specific rights and a plausible return for the amount asked.

### brand_safety

- Source: umbrella 3 (reputation, equity); umbrella 16 (regulatory and
  marketing governance); "What could go wrong?"
- 1: Obvious exposure: a contested cause, an unknown organiser, regulated claims.
- 3: No evident exposure, but the organiser or event is unverified.
- 5: An established organiser and an uncontroversial setting.

### activation_potential

- Source: umbrella 20 (develop activation ideas); "Is it distinctive?"
- 1: Logo placement only.
- 3: Some room to do more than appear.
- 5: A clear chance to do something only this brand would do.

## Risk flags

Set a flag only with a reason taken from the enquiry.

- reputational: association the company would have to explain or defend.
- political: a party, candidate, campaign, government contest or contested
  public cause.
- legal: regulated claims, gambling, unlicensed financial products, or terms
  that bind the company.

Any flag sends the enquiry to the owner with no reply drafted.

## Outside the criteria

Mark an enquiry outside the criteria when no score could bring it in: personal
fundraising, requests from individuals with no organisation, or categories the
company does not sponsor.

## Tone rules

- Concise, intelligent, human, senior, non-cheesy, commercially grounded.
- Plain text. No markdown, no exclamation marks, no filler openers.
- Address the sender by name when the enquiry gives one.
- Never state a figure, date, name or commitment that is not in the enquiry.
- Never promise funding, and never suggest a decision has been made.
- Sign off as the owner's office; no job titles.

## Reply templates

### Pursue

Express real interest in one sentence, naming what makes the opportunity fit.
Ask the sender for two dates that suit them for a call. Three to five sentences.
(The calendar is not connected yet, so the reply asks for dates rather than
proposing them.)

### Ask for detail

Thank them in one sentence, then list the specific questions, at most five, one
per line. Each question must come from the missing information. Say a decision
follows once the answers are in.

### Decline

A courteous decline in two to four sentences. Give fit as the only reason. No
criticism of the event, no mention of budget, scores or risk, and no invitation
to resubmit unless the enquiry gives a reason to offer one.
