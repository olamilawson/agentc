# Journey maps

Three people use Ijeoma.Pro. Each gets one map, stage by stage. Every step is
marked against the code as it stands (commit 7a60118):

- **Works**: built and tested.
- **Broken**: built, but the person hits a wall or a dead end.
- **Missing**: nothing exists for this step.

The feelings are our assumptions, not research. Nobody has interviewed a client
yet. Check them against the three real requests already waiting.

---

## 1. A new client

A marketing lead at a brand who has heard of Chidimma and wants to hire her.

| Stage | What they do | Where | How they likely feel | Status |
|---|---|---|---|---|
| Awareness | Reads about Chidimma, clicks "Meet Ijeoma.Pro" | chidimma.com | Curious. Unsure whether the agent replaces her | Works (link live) |
| Consideration | Reads the request page, picks a topic | ijeoma.pro/enquire | Wary of talking to a bot instead of her | Works |
| Request | Fills name, email and topic, sends | /enquire | Fine, the form is short | Works |
| Waiting | Waits for an answer | Their inbox | Anxious. Nothing says when to expect a reply | **Broken**: the "sent" page names no time frame, and no confirmation email goes out |
| Invited | Gets an email with a private link | Inbox, then /brief/{token} | Relieved | **Broken** until Outlook is connected: invites wait in the outbox and nobody receives them |
| Brief | Writes the brief, attaches files, sends | /brief/{token} | Hopeful, slightly unsure what detail to give | Works |
| Reply | Gets Chidimma's reply after she approves it | Inbox | Valued, if it arrives within days | Works on approval; **Broken** while no mailbox is connected |
| Engagement | Books a call | Booking link | Ready to commit | **Missing**: `booking_url` is blank, so the reply has no link |

Measure: requests per week, request to invite, invite to brief sent, days from
brief to reply.

## 2. A returning client

Someone who has already sent a brief and wants to send another one, check on
the first, or add a file.

| Stage | What they do | Where | How they likely feel | Status |
|---|---|---|---|---|
| Come back | Opens their old link | /brief/{token} | Expects to get back in | **Broken**: it says "Your brief reached me" and nothing more. No way forward |
| Come back | Searches their inbox for a way in | Inbox | Confused | **Missing**: no sign-in, no "send me a fresh link" |
| Ask again | Fills the public request form again | /enquire | Annoyed at being treated as a stranger | **Broken**: the form does not recognise their email. They queue behind strangers, and the screening runs again |
| See history | Looks for their past briefs and replies | — | Wants a record | **Missing**: clients have no page of their own |
| Add to a brief | Sends an extra file or a correction | — | Wants to fix a mistake | **Missing** |

Measure: share of requests from known emails, time from a returning request to
a new link.

## 3. Chidimma, the owner

She runs her practice through the agent and wants it to do the routine work.

| Stage | What she does | Where | How she likely feels | Status |
|---|---|---|---|---|
| Start the day | Opens the app to see what needs her | /today | Wants one list | Works, but requests, approvals and tasks sit on separate pages |
| Ask the agent | Types a question, then a follow-up | /think | Expects a conversation | Works: conversations with follow-ups, listed beside the thread |
| Give it material | Attaches a document to a question | /think | Expects a paperclip | Works: PDF, Word, text and CSV files, read on every later turn. Images are not read yet. Tasks on /new still take files per form |
| Handle requests | Reads requests, sets rules, approves templates | /enquiries | Wants it to run itself | Works (deployed? see below). The rules start empty, so nothing is screened until she writes them |
| Approve work | Approves or edits replies and assessments | /queue | Wants one tap | Works |
| Follow up | Checks whether a client replied | — | Wants it tracked | **Missing**: replies to her emails do not come back into the app |

Measure: minutes per day in the app, approvals per day, the share of requests
handled without her.

Deploy note: the server is still on 751099a, so "How the agent handles
requests" is not live yet.

---

## Gaps in priority order

Ordered by how many people hit them and how often.

1. **Owner chat with attachments.** Built: conversations with follow-ups and
   a paperclip. Still to come: the agent starting tasks from the chat, and
   reading images.
2. **Returning clients get back in.** Recognise a known email. Send a fresh
   private link by email ("Email me a new link") with no password. Give each
   client a page of their briefs and replies behind that link.
3. **Connect Outlook.** Without it, invites and replies never leave. This is
   setup, not code: `./deploy/setup.sh --mailbox`.
4. **Set expectations while clients wait.** Give a reply time on the "sent"
   page, send a confirmation email, and add the booking link.
5. **One inbox for the owner.** Requests, approvals and tasks in one list on
   /today.
6. **Client replies come back.** Thread incoming email to the client and the
   run.
