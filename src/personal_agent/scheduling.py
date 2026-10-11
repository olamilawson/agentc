"""Finding times to propose for a call (PRD: sponsorship triage / pursue route).

The pursue reply proposes two dates for a call. This module picks them in code
from the owner's free and busy periods and the owner's stated preferences
(config/calendar.yaml): no model chooses a time, and the only thing read from
the calendar is whether a period is busy, never what the meeting is.
"""

from __future__ import annotations

from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from pydantic import BaseModel, Field

WEEKDAYS = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")
MONTHS = ("January", "February", "March", "April", "May", "June", "July", "August", "September",
          "October", "November", "December")


class CalendarPolicy(BaseModel):
    """When the owner takes calls (config/calendar.yaml)."""

    timezone: str = "Africa/Lagos"
    working_days: list[int] = Field(default=[0, 1, 2, 3, 4])  # Monday is 0
    day_start: str = "10:00"
    day_end: str = "16:00"
    call_minutes: int = 30
    earliest_days_ahead: int = 2  # never propose today or tomorrow
    look_ahead_days: int = 14
    # Ask Microsoft to attach a Teams meeting to an event the agent creates.
    online_meeting: bool = False


def _clock(text: str) -> time:
    hour, minute = text.split(":")
    return time(int(hour), int(minute))


def search_window(policy: CalendarPolicy, now: datetime) -> tuple[datetime, datetime]:
    """The stretch of calendar worth reading, in UTC."""
    zone = ZoneInfo(policy.timezone)
    first = (now.astimezone(zone) + timedelta(days=policy.earliest_days_ahead)).date()
    last = first + timedelta(days=policy.look_ahead_days)
    start = datetime.combine(first, time(0, 0), zone)
    end = datetime.combine(last, time(23, 59), zone)
    return start.astimezone(timezone.utc), end.astimezone(timezone.utc)


def label(policy: CalendarPolicy, start: datetime) -> str:
    """A time as the reply states it, with the zone spelled out for the reader."""
    zone = ZoneInfo(policy.timezone)
    local = start.astimezone(zone)
    end = local + timedelta(minutes=policy.call_minutes)
    offset = local.utcoffset() or timedelta(0)
    hours = offset.total_seconds() / 3600
    utc = "UTC" + (f"{hours:+g}" if hours else "")
    place = policy.timezone.split("/")[-1].replace("_", " ")
    return (
        f"{WEEKDAYS[local.weekday()]} {local.day} {MONTHS[local.month - 1]} {local.year}, "
        f"{local:%H:%M} to {end:%H:%M} {place} time ({utc})"
    )


def find_slots(
    policy: CalendarPolicy,
    busy: list[tuple[datetime, datetime]],
    now: datetime,
    taken: set[str] = frozenset(),
    count: int = 2,
) -> list[dict]:
    """Up to `count` free call times, each on a different day: the first free
    time of each working day inside the owner's hours. `taken` holds the start
    times (ISO, UTC) already offered to someone else."""
    zone = ZoneInfo(policy.timezone)
    step = timedelta(minutes=policy.call_minutes)
    today = now.astimezone(zone).date()
    slots: list[dict] = []
    for ahead in range(policy.earliest_days_ahead, policy.earliest_days_ahead + policy.look_ahead_days + 1):
        day = today + timedelta(days=ahead)
        if day.weekday() not in policy.working_days:
            continue
        start = datetime.combine(day, _clock(policy.day_start), zone)
        closing = datetime.combine(day, _clock(policy.day_end), zone)
        while start + step <= closing:
            end = start + step
            start_utc = start.astimezone(timezone.utc)
            key = start_utc.isoformat(timespec="minutes")
            if key not in taken and not any(b_start < end and start < b_end for b_start, b_end in busy):
                slots.append({"start": key, "label": label(policy, start_utc)})
                break
            start += step
        if len(slots) == count:
            break
    return slots


def still_free(policy: CalendarPolicy, slots: list[dict], busy: list[tuple[datetime, datetime]], now: datetime) -> bool:
    """True if every proposed time is still in the future and still free."""
    step = timedelta(minutes=policy.call_minutes)
    for slot in slots:
        start = datetime.fromisoformat(slot["start"])
        if start <= now or any(b_start < start + step and start < b_end for b_start, b_end in busy):
            return False
    return True
