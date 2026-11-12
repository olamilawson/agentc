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
