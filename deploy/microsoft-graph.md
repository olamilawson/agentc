# Connecting the mailbox through Microsoft Graph

The agent sends approved replies from one Outlook mailbox, reads earlier mail
with a sender in that mailbox, and reads free and busy times from your
calendar so a reply can propose a call. It signs in as a registered application,
with no person's password. A tenant administrator does steps 1 to 4; you do
steps 5 to 7 on the server.

Until all four settings in step 5 exist, the agent holds approved replies in
its local outbox and sends none.

## 1. Register the application

In the Microsoft Entra admin center, open App registrations and choose New
registration. Give it a name such as "Personal agent mail" and keep it to this
organisation's directory. Note two values from its overview page:

- Application (client) ID
- Directory (tenant) ID

## 2. Create a client secret

Open Certificates & secrets, add a client secret, and copy its Value at once;
the portal shows it one time. Note the expiry date. The agent stops sending on
that date until you replace the secret.

## 3. Grant three permissions

Open API permissions, add Microsoft Graph permissions of the **Application**
type, and choose:

- `Mail.ReadWrite`: read mail and build the reply as a draft
- `Mail.Send`: send it
- `Calendars.Read`: read free and busy times

Then choose Grant admin consent.

## 4. Limit the application to the one mailbox

With step 3 alone, the application can read and send as each mailbox in the
organisation. Limit it before the secret goes anywhere.

Exchange Online offers two ways: an application access policy, or role-based
access control for applications. Microsoft is moving from the first to the
second, so the administrator should follow Microsoft's current guidance. With
an application access policy the commands are:

```powershell
New-ApplicationAccessPolicy -AppId <application-client-id> `
  -PolicyScopeGroupId <mail-enabled-security-group-holding-the-mailbox> `
  -AccessRight RestrictAccess -Description "Personal agent: sponsorship mailbox"
```

```powershell
Test-ApplicationAccessPolicy -AppId <application-client-id> -Identity <another-mailbox-address>
```

The test must report `Denied` for a mailbox outside the group. If your calendar
lives in a different mailbox from the one the agent sends from, put both in
the group.

## 5. Add the settings on the server

In `/opt/personal-agent/.env`:

| Line | Value |
|---|---|
| `MS_GRAPH_TENANT_ID` | Directory (tenant) ID |
| `MS_GRAPH_CLIENT_ID` | Application (client) ID |
| `MS_GRAPH_CLIENT_SECRET` | the secret's Value |
| `MS_GRAPH_MAILBOX` | the mailbox address, for example the sponsorship alias |
| `MS_GRAPH_CALENDAR` | the address whose calendar holds your meetings; leave empty to use the mailbox |

Then restart:

```bash
cd /opt/personal-agent && docker compose up -d
```

## 6. Confirm

Open Settings and cost. Under Connected tools it reads "Mail goes through
Microsoft Graph from" followed by the mailbox address.

The first real proof is one reply. Send a test enquiry to the mailbox from an
address you own, approve the agent's reply, and read it in your own inbox and
in the mailbox's Sent Items.

## 7. Point the n8n watcher at the agent

n8n watches the mailbox with its own Outlook credential and posts each new
mail to `https://ijeoma.pro/hooks/n8n/sponsorship` with the header
`x-agent-secret`. Map the fields this way:

| Field the agent expects | From the Outlook message |
|---|---|
| `message_id` | `id` (the Graph message id, so the reply joins the thread) |
| `sender` | `from.emailAddress.address` |
| `sender_name` | `from.emailAddress.name` |
| `subject` | `subject` |
| `body` | the body as plain text |
| `received_at` | `receivedDateTime` |

## How a send behaves

- The agent builds the reply as a draft in the mailbox, sets the recipient,
  subject and body to the approved content, and sends that draft. A Reply-To
  header on the original cannot redirect it.
- If Microsoft refuses before the send, the agent sends nothing. The reply
  returns to your queue with the reason, and a second approval tries again.
- If the connection drops during the send itself, the outcome is unknown. The
  agent stops the run, records that on the ledger, and does not retry. Check
  Sent Items before you do anything else.
- If the mailbox no longer holds the original message, the reply goes as a new
  message to the same address.

## How a proposed call time behaves

- On the pursue route the agent reads your free and busy times and picks two
  free times in code, on different days, inside the hours in
  `config/calendar.yaml`. It reads no meeting's subject or attendees.
- The reply states each time with its zone, for example "Wednesday 14 October
  2026, 10:00 to 10:30 Lagos time (UTC+1)".
- The agent does not hold the times in your calendar. It reads the calendar
  again at approval; if a time has gone, it drafts the reply again with new
  times and asks for a new approval.
- A time offered in one reply is not offered in another.
- With no calendar connection, an unreadable calendar, or fewer than two free
  times, the reply asks the sender for dates.

## Limits

- One application holds read, draft and send. The gateway's approval rule is
  what keeps a send to approved content; the permissions alone do not.
- The agent creates no calendar event. Once the sender picks a time, you add
  the meeting yourself.
- The agent knows no public holidays. Block them in your calendar and it
  will skip them.
- Replies held in the local outbox before the connection existed stay there.
  The agent does not send them afterwards.
