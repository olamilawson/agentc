# State machines

Each machine below has one status column and one module that moves it. Pages
show every state in words and carry it as a `data-state` attribute.

## Client enquiry

Table `enquiries`, column `status`. Transitions live in
`src/personal_agent/engine/enquiries.py` and nowhere else; each one is a
compare-and-set (`Database.claim_enquiry`), so two requests cannot both win.

```
public page --receive()------> received --invite()--> invited --last brief in--> briefed
                                  |                     |  \--expires_at passes, settle()--> expired
                                  +--decline()--> declined  \--cancel()-------------------> cancelled
owner --invite_direct()------------------------------> invited
```

| State | Meaning | The person's link shows |
|---|---|---|
| `received` | A request from the public page waits for the owner | (no link yet) |
| `invited` | The link is live: it opens the brief form until `expires_at`, for `briefs_left` briefs | the brief form (`live`) |
| `briefed` | The link took its last brief | "Your brief reached me" |
| `declined` | The owner said no; nothing was sent | (no link) |
| `cancelled` | The owner withdrew the link | "This link is no longer active" |
| `expired` | The link's time passed unused | "This link has expired" |

`expired` is set lazily by `settle()`, which runs before enquiries are listed
and before a link opens. Declined, cancelled and expired rows are deleted after
`keep_days` (config/enquiries.yaml) by the retention job.

Observe: the owner's screen `/enquiries` (rows carry `data-state`), the
detail page `/enquiries/{id}`, and the public page `/brief/{token}` (its
outcome block carries `data-state`). The audit log records `enquiry_received`,
`enquiry_invited`, `enquiry_declined`, `enquiry_link_cancelled`,
`enquiry_link_expired`, `enquiry_brief_received` and `enquiries_forgotten`.

### Screening a request

Table `enquiry_screenings`, one row per enquiry, written by
`screen_request()` in `engine/enquiries.py` after the `enquiry-screen` run.
`decision` is `invite | decline | ask`; `acted` is `invited | declined |
waiting`, by the owner's "decide clear cases" setting. No row: not screened
(no rules written, the daily limit, or the monthly cap; the audit log says
which). A request with instruction-like text is never invited automatically.
Observe: the verdict chip on `/enquiries` and the evaluation panel on
`/enquiries/{id}` (`data-state` = the decision).

### A template email

Setting `template:<kind>` in `owner_settings`. State, computed by
`template()`: `standing` (approved and on, and the words still hash to what
she approved), `changed` (approved once, words differ), `off`. Only `standing`
sends without asking; each such send records `standing_approval_used`. The
form carries `data-state`.

### A brief inside an enquiry

Each entry in `enquiries.briefs` has a `run_id`. `None` means the brief came in
past the month's allowance (`evaluations_per_month`) or the spending cap, and
waits for the owner's "Evaluate now"; otherwise its state is the run's state
below.

### The request form and the brief form

The `<form class="pform">` carries `data-state`: `open`, `error` (the server
returned it with gaps named), `sending` (set by the page as it submits, so a
second press does nothing) and, on the request page, `closed` when
`waiting_cap` requests already wait. The brief form also carries `data-step`
(1 for details, 2 for the brief).

## A conversation on Think

Table `chats`; each message is a `think` run whose `inputs.chat_id` names the
conversation, so a turn's state is its run's state. Files attached to it are
rows in `chat_files` (the text read from them, nothing else). The workstream is
fixed when the conversation starts. Routes live in `api/main.py` (`/think`);
what a turn reads is decided in `engine/think.py` (`conversation_material`).

```
composer:  ready --send--> sending (the page, as it submits) --> waiting (last turn queued or running)
           waiting --answer in--> ready
           waiting --run failed--> ready, with "Send again" on the failed turn
```

The server refuses a message to a conversation whose last turn is still
working, so one answer is in progress at a time. A file the agent cannot read
refuses the whole message, and nothing is sent.

Observe: `GET /think/{chat_id}/state` returns `{state: waiting | ready,
turns, last_turn: {id, status}}`; the page polls it while waiting. The
composer form and each turn carry `data-state`; the disabled Send button names
its reason through `aria-describedby`. A client's retention period and its
deletion remove its conversations and their files.

## Run

Table `runs`, column `status`, moved by `engine/runner.py`.
`queued -> running -> waiting_for_approval | waiting_for_input -> running -> done | failed | cancelled`.
A release (a send, a booking confirmation) happens only after the owner
approves the exact content; see `engine/approval.py`.

## Agency dispatch

Table `dispatches`, column `status`, moved by `engine/dispatch.py` through
`Database.claim_dispatch`. `held -> sending -> sent | outbox | failed | uncertain`;
`held -> recalled`; `sent -> halt_pending -> halted` (or back to `sent` if the
owner rejects the halt notice). Labels: `STATES` in `api/dispatches.py`.
