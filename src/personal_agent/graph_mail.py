"""Microsoft Graph mail (PRD: tools and integrations / mail).

The agent's own connection to one Outlook mailbox, through a registered
application that signs in as itself (client credentials). Two things use it:

- send_reply: the release-class send. It builds the reply as a draft in the
  mailbox, sets the recipient, subject and body to exactly what the owner
  approved, and sends that draft. The recipient comes from the approved
  content, never from the original message's Reply-To header.
- search_correspondence: the read-class lookup of earlier mail with a sender.
- create_event: the release-class creation of a calendar event with an
  attendee. Microsoft sends the invitation as soon as the event exists, so
  the one call that creates it cannot be taken back.
- change_event and cancel_event: release-class too. Microsoft tells the
  attendee about a new time or a cancellation as soon as it is made.
- busy_periods: the read-class lookup of when a calendar is busy. It asks for
  free and busy times only, so no meeting's subject or attendees reach the agent.

n8n still watches the mailbox; this module never polls it.

Sending has one call that cannot be taken back. A network failure or a server
error on that call leaves the outcome unknown, and the harness must not retry:
it raises UncertainOutcome and the owner checks Sent Items. Every failure
before that call is definite: nothing was sent.
"""

from __future__ import annotations

import base64
import time
from datetime import datetime, timezone
from urllib.parse import quote

import httpx

from personal_agent.gateway.tools import UncertainOutcome

GRAPH = "https://graph.microsoft.com/v1.0"
LOGIN = "https://login.microsoftonline.com"


class GraphError(Exception):
    """A definite failure: Microsoft refused or could not be reached before
    anything irreversible happened. Nothing was sent."""


def _reason(response: httpx.Response) -> str:
    try:
        body = response.json()
    except ValueError:
        return f"HTTP {response.status_code}"
    error = body.get("error")
    if isinstance(error, dict):  # Graph: {"error": {"code", "message"}}
        return f"{error.get('code', response.status_code)}: {str(error.get('message', ''))[:300]}"
    return f"{error or response.status_code}: {str(body.get('error_description', ''))[:300]}"  # the token endpoint


class GraphMail:
    def __init__(self, tenant_id: str, client_id: str, client_secret: str, mailbox: str,
                 transport: httpx.BaseTransport | None = None, timeout: float = 30.0,
                 calendar: str | None = None):
        self.tenant_id, self.client_id, self._secret, self.mailbox = tenant_id, client_id, client_secret, mailbox
        self.calendar = calendar or mailbox  # whose free time to read
        self._http = httpx.Client(transport=transport, timeout=timeout)
        self._token: str | None = None
        self._token_expires = 0.0

    # --- sign-in for the application ---------------------------------------------------
    def _bearer(self) -> str:
        if self._token and time.time() < self._token_expires:
            return self._token
        try:
            response = self._http.post(
                f"{LOGIN}/{quote(self.tenant_id)}/oauth2/v2.0/token",
                data={
                    "grant_type": "client_credentials",
                    "client_id": self.client_id,
                    "client_secret": self._secret,
                    "scope": "https://graph.microsoft.com/.default",
                },
            )
        except httpx.HTTPError as e:
            raise GraphError(f"Microsoft sign-in could not be reached: {type(e).__name__}") from e
        if response.status_code != 200:
            raise GraphError(f"Microsoft refused the application's sign-in ({_reason(response)})")
        body = response.json()
        self._token = body["access_token"]
        self._token_expires = time.time() + int(body.get("expires_in", 3600)) - 60
        return self._token

    def _call(self, method: str, path: str, ok: tuple[int, ...], allow: tuple[int, ...] = (), **kwargs) -> httpx.Response:
        """One Graph call that is safe to fail: any problem is a definite GraphError."""
        headers = {"Authorization": f"Bearer {self._bearer()}", **kwargs.pop("headers", {})}
        try:
            response = self._http.request(method, f"{GRAPH}{path}", headers=headers, **kwargs)
        except httpx.HTTPError as e:
            raise GraphError(f"Microsoft Graph could not be reached: {type(e).__name__}") from e
        if response.status_code not in ok + allow:
            raise GraphError(f"Microsoft Graph refused the request ({_reason(response)})")
        return response

    def _mailbox_path(self) -> str:
        return f"/users/{quote(self.mailbox)}"

    # --- release: send a reply ------------------------------------------------------------
    def send_reply(self, to: str, subject: str, body: str, in_reply_to: str | None = None,
                   attachments: list[tuple[str, bytes]] | None = None) -> dict:
        """Send exactly this subject and body to exactly this address.

        With in_reply_to (the Graph id of the message being answered) the reply
        joins that conversation. If the mailbox no longer holds that message,
        the reply goes as a new message and the result says so.
        """
        base = self._mailbox_path()
        content = {
            "subject": subject,
            "body": {"contentType": "Text", "content": body},
            # Set outright, so a Reply-To header on the original cannot redirect the reply.
            "toRecipients": [{"emailAddress": {"address": to}}],
            "ccRecipients": [],
            "bccRecipients": [],
        }
        draft_id, threaded = None, False
        if in_reply_to:
            made = self._call("POST", f"{base}/messages/{quote(in_reply_to, safe='')}/createReply", ok=(201,), allow=(404,))
            if made.status_code == 201:
                draft_id, threaded = made.json()["id"], True
        try:
            if draft_id:
                self._call("PATCH", f"{base}/messages/{quote(draft_id, safe='')}", ok=(200,), json=content)
            else:
                draft_id = self._call("POST", f"{base}/messages", ok=(201,), json=content).json()["id"]
            # Files go onto the draft before it is sent; a refusal here leaves nothing sent.
            for name, data in attachments or []:
                self._call("POST", f"{base}/messages/{quote(draft_id, safe='')}/attachments", ok=(201,), json={
                    "@odata.type": "#microsoft.graph.fileAttachment", "name": name,
                    "contentBytes": base64.b64encode(data).decode(),
                })
        except GraphError:
            self._discard(draft_id)
            raise

        # The one call that cannot be taken back.
        send_path = f"{GRAPH}{base}/messages/{quote(draft_id, safe='')}/send"
        try:
            sent = self._http.post(send_path, headers={"Authorization": f"Bearer {self._bearer()}"})
        except GraphError:
            self._discard(draft_id)
            raise
        except httpx.HTTPError as e:
            raise UncertainOutcome(
                f"the connection failed while Microsoft was sending the reply ({type(e).__name__}); "
                "it may or may not have gone. Check Sent Items before doing anything else"
            ) from e
        if sent.status_code == 202:
            return {"sent": True, "via": "Microsoft Graph", "mailbox": self.mailbox, "to": to, "threaded": threaded}
        if sent.status_code >= 500:
            raise UncertainOutcome(
                f"Microsoft answered {sent.status_code} while sending the reply; it may or may not have gone. "
                "Check Sent Items before doing anything else"
            )
        self._discard(draft_id)
        raise GraphError(f"Microsoft Graph refused to send the reply ({_reason(sent)})")

    def _discard(self, draft_id: str | None) -> None:
        """Remove a draft that will not be sent. Best effort: a leftover draft is harmless."""
        if not draft_id:
            return
        try:
            self._call("DELETE", f"{self._mailbox_path()}/messages/{quote(draft_id, safe='')}", ok=(204,), allow=(404,))
        except GraphError:
            pass

    # --- read: earlier correspondence -------------------------------------------------------
    def search_correspondence(self, address: str, limit: int = 10) -> list[dict]:
        """Recent mail to or from an address: when, who, subject and a short preview."""
        response = self._call(
            "GET", f"{self._mailbox_path()}/messages", ok=(200,),
            params={
                "$search": f'"participants:{address}"',
                "$top": str(limit),
                "$select": "id,subject,receivedDateTime,from,bodyPreview",
            },
        )
        return [
            {
                "id": m.get("id"),
                "received_at": m.get("receivedDateTime"),
                "from": ((m.get("from") or {}).get("emailAddress") or {}).get("address"),
                "subject": m.get("subject"),
                "preview": (m.get("bodyPreview") or "")[:300],
            }
            for m in response.json().get("value", [])
        ]


    # --- release: create an event with an attendee -------------------------------------------
    def create_event(self, subject: str, start: datetime, end: datetime, attendee: str, note: str,
                     transaction_id: str, online: bool = False) -> dict:
        """Put the event in the calendar and invite the attendee. Microsoft sends
        the invitation itself. transaction_id makes a repeat of the same request
        return the same event instead of creating a second one."""
        stamp = lambda moment: moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")  # noqa: E731
        event = {
            "subject": subject,
            "body": {"contentType": "Text", "content": note},
            "start": {"dateTime": stamp(start), "timeZone": "UTC"},
            "end": {"dateTime": stamp(end), "timeZone": "UTC"},
            "attendees": [{"emailAddress": {"address": attendee}, "type": "required"}],
            "transactionId": transaction_id,
        }
        if online:
            event.update({"isOnlineMeeting": True, "onlineMeetingProvider": "teamsForBusiness"})
        try:
            token = self._bearer()  # a failed sign-in is definite: nothing was created
            made = self._http.post(
                f"{GRAPH}/users/{quote(self.calendar)}/events", headers={"Authorization": f"Bearer {token}"}, json=event
            )
        except httpx.HTTPError as e:
            raise UncertainOutcome(
                f"the connection failed while Microsoft was creating the event ({type(e).__name__}); "
                "the invitation may or may not have gone. Check the calendar before doing anything else"
            ) from e
        if made.status_code in (200, 201):
            body = made.json()
            return {"created": True, "via": "Microsoft Graph", "calendar": self.calendar, "attendee": attendee,
                    "event_id": body.get("id"), "link": body.get("webLink")}
        if made.status_code >= 500:
            raise UncertainOutcome(
                f"Microsoft answered {made.status_code} while creating the event; the invitation may or may "
                "not have gone. Check the calendar before doing anything else"
            )
        raise GraphError(f"Microsoft Graph refused to create the event ({_reason(made)})")

    def _irreversible(self, method: str, path: str, ok: tuple[int, ...], doing: str, **kwargs) -> httpx.Response:
        """A call that changes what an attendee sees. A refusal is definite; a
        dropped connection or a server error leaves the outcome unknown."""
        token = self._bearer()  # a failed sign-in is definite: nothing was changed
        try:
            response = self._http.request(
                method, f"{GRAPH}{path}", headers={"Authorization": f"Bearer {token}"}, **kwargs
            )
        except httpx.HTTPError as e:
            raise UncertainOutcome(
                f"the connection failed while Microsoft was {doing} ({type(e).__name__}); it may or may not "
                "have happened. Check the calendar before doing anything else"
            ) from e
        if response.status_code in ok:
            return response
        if response.status_code >= 500:
            raise UncertainOutcome(
                f"Microsoft answered {response.status_code} while {doing}; it may or may not have happened. "
                "Check the calendar before doing anything else"
            )
        if response.status_code == 404:
            raise GraphError("the event is no longer in the calendar")
        raise GraphError(f"Microsoft Graph refused ({_reason(response)})")

    def change_event(self, event_id: str, start: datetime, end: datetime) -> dict:
        """Move an event to a new time. Microsoft sends the attendee the update."""
        stamp = lambda moment: moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")  # noqa: E731
        self._irreversible(
            "PATCH", f"/users/{quote(self.calendar)}/events/{quote(event_id, safe='')}", (200,), "moving the event",
            json={"start": {"dateTime": stamp(start), "timeZone": "UTC"},
                  "end": {"dateTime": stamp(end), "timeZone": "UTC"}},
        )
        return {"changed": True, "via": "Microsoft Graph", "calendar": self.calendar, "event_id": event_id}

    def cancel_event(self, event_id: str, comment: str) -> dict:
        """Cancel an event. Microsoft sends the attendee the cancellation with the comment."""
        self._irreversible(
            "POST", f"/users/{quote(self.calendar)}/events/{quote(event_id, safe='')}/cancel", (202,),
            "cancelling the event", json={"comment": comment},
        )
        return {"cancelled": True, "via": "Microsoft Graph", "calendar": self.calendar, "event_id": event_id}

    # --- read: when the calendar is busy ----------------------------------------------------
    def busy_periods(self, start: datetime, end: datetime) -> list[tuple[datetime, datetime]]:
        """The periods between start and end when the calendar's owner is not
        free, as (start, end) in UTC. Tentative and out-of-office count as busy."""
        stamp = lambda moment: moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")  # noqa: E731
        response = self._call(
            "POST", f"/users/{quote(self.calendar)}/calendar/getSchedule", ok=(200,),
            json={
                "schedules": [self.calendar],
                "startTime": {"dateTime": stamp(start), "timeZone": "UTC"},
                "endTime": {"dateTime": stamp(end), "timeZone": "UTC"},
                "availabilityViewInterval": 30,
            },
        )
        schedules = response.json().get("value") or [{}]
        if schedules[0].get("error"):
            raise GraphError(f"Microsoft Graph could not read the calendar ({schedules[0]['error'].get('message', '')[:200]})")

        def moment(value: dict) -> datetime:
            return datetime.fromisoformat(value["dateTime"].split(".")[0]).replace(tzinfo=timezone.utc)

        return [
            (moment(item["start"]), moment(item["end"]))
            for item in schedules[0].get("scheduleItems", [])
            if item.get("status") not in ("free", "workingElsewhere")
        ]


def from_settings(settings) -> GraphMail | None:
    """The connection, if all four values are set; otherwise None and mail stays local."""
    values = (settings.graph_tenant_id, settings.graph_client_id, settings.graph_client_secret, settings.graph_mailbox)
    return GraphMail(*values, calendar=settings.graph_calendar) if all(values) else None
