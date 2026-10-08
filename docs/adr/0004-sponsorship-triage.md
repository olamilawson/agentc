# ADR 0004: Sponsorship triage — what is decided in code, and what stands in for missing connections

Date: 2026-10-09
Decided by: Builder
Status: Accepted; several points are provisional until the owner's open decisions are settled

## The route is decided in code

The model scores the enquiry against the criteria, names risk flags and lists
missing information. It does not choose the route. `decide_route` in
`engine/sponsorship_triage.py` applies the PRD's route table with the
thresholds from `config/sponsorship.yaml`, escalation first:

1. Any risk flag (reputational, political, legal) escalates.
2. An amount above the owner's limit escalates. The amount is read twice: the
   model's extraction, and a regular expression over the mail itself. The
   larger wins, so a mail that talks the model into understating the amount
   still escalates. An amount that cannot be read as naira escalates.
3. Outside the criteria, or a mean score below the decline threshold: decline.
4. Information missing: ask for detail.
5. Mean score at or above the pursue threshold: pursue.
6. Anything left (between the thresholds, nothing missing) escalates. The PRD's
   table does not cover this case; sending it to the owner is the safe reading.

With no policy file, every enquiry escalates. With no amount limit, every
enquiry that names an amount escalates.

## Provisional inputs

The owner has not yet set the criteria, thresholds or amount limit (PRD open
decisions). `skills/sponsorship-triage/SKILL.md` and `config/sponsorship.yaml`
are builder-written and marked provisional so the graph could be built and
tested. `evaluation/sets/sponsorship-triage.jsonl` is synthetic and never
satisfies the release gate.

## Mail is not connected

There is no Microsoft Graph application yet (mailbox and tenant consent are
open decisions). Until there is:

- `send_mail` is registered as a release-class tool with the real approval and
  idempotency rules, but its function writes the approved reply to a local
  `outbox` table and returns `sent: false`. The run record and the ledger say
  the reply is held, not sent. The Graph tool replaces the function only.
- "Look up history" reads the sponsorship ledger. Earlier correspondence in the
  mailbox joins when the read-mail tool exists.
- The pursue reply asks the sender for two dates instead of proposing them,
  because proposing dates needs the calendar. This departs from the PRD's
  route table and reverts when the calendar read tool exists.

## Other choices

- The reply address is the envelope sender from the webhook, fixed in the first
  node. Nothing extracted from the mail body can change it, and the address is
  part of the content the approval hash covers.
- An escalation appears in the approval queue as a summary with the original
  mail; the owner acknowledges it and nothing is sent.
- Check reply first compares the reply's figures and addresses with the mail in
  code, then asks a model about tone and route. Open faults after two returns
  are shown beside the reply in the queue, never inside the content to be sent.
- A mail classified as not a sponsorship request ends the run with no reply, no
  queue item and no ledger row; the reason is on the run record.
- The n8n webhook runs the graph inside the request. The PRD's queue in
  PostgreSQL with a separate worker is not built yet.
