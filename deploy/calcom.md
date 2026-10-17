# Booking calls through Cal.com

People book a time on your Cal.com page; each booking waits in Review for your
approval before Cal.com confirms it. Nothing is booked in your name without
you.

This uses hosted Cal.com (app.cal.com), so nothing extra runs on this server.

## 1. In Cal.com

1. Sign up at cal.com and connect your calendar there (Settings, Calendars).
   Cal.com then offers only times your calendar shows as free.
2. Make an event type, for example "Sponsorship call, 30 minutes".
3. In that event type's Advanced settings, turn on **Requires confirmation**.
   This is the step that keeps you in control: without it, a booking is made
   at once and the agent can only record it.
4. Under Settings, Developer, API keys, create a key. Copy it.

## 2. On the server

```bash
cd /opt/personal-agent && git pull && ./deploy/setup.sh --key calcom
```

It asks for the API key, hidden as you paste it, then makes a webhook secret
and prints it once with the webhook address.

## 3. Back in Cal.com

Settings, Developer, Webhooks, New:

- Subscriber URL: `https://ijeoma.pro/hooks/calcom`
- Secret: the one the command printed
- Events: Booking requested, Booking created, Booking rescheduled, Booking
  cancelled, Booking rejected

## What happens

| In Cal.com | In the agent |
|---|---|
| Someone books a call that needs confirmation | It waits in Review with the time, their name, their note, and whether your Outlook calendar is free then (when the mailbox is connected) |
| You approve it in Review | The agent confirms it through Cal.com; Cal.com tells them; it appears on Calls |
| You reject it in Review | The agent declines it, with your comment as the reason |
| You confirm or decline it in Cal.com itself | The item leaves Review |
| A booking with no confirmation step is made | It appears on Calls |
| A booking is rescheduled or cancelled | Calls follows |

Move or cancel a Cal.com booking in Cal.com. The agent's own move and cancel
are for calls it booked through your mailbox.

## Limits

- Without `CALCOM_API_KEY`, the agent still records bookings, but approving one
  fails with a reason; answer it in Cal.com instead.
- The name and note someone types are their words. The agent shows them and
  acts on nothing in them.
- Written from Cal.com's published webhook and API references in October 2026,
  and not yet run against a live account. Cal.com's documentation does not say
  whether its signature is hex or base64; the agent accepts either.
