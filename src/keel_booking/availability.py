"""The slot engine: which start times a meeting type offers between two instants.

A slot is offered when all of these hold:

1. it falls inside the type's hours for that date (a date override, else the weekly hours),
   read as wall-clock time in the type's timezone;
2. it starts at least ``min_notice_minutes`` from now and within ``max_days_ahead`` days;
3. the slot, widened by the type's own buffers, overlaps no busy range: no event on the
   checked Google calendars and no other confirmed booking (of any type, widened by that
   booking's buffers), because the owner is one person;
4. the type's ``daily_limit`` for that date is not yet reached.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone as dt_timezone

from django.utils import timezone

from .models import Booking, EventType

UTC = dt_timezone.utc


@dataclass(frozen=True)
class Slot:
    start: datetime
    end: datetime


def _windows_for(event_type: EventType, day: date, weekly: dict, overrides: dict) -> list[tuple[time, time]]:
    rows = overrides.get(day)
    if rows:
        if any(row.is_unavailable for row in rows):
            return []
        return [(row.start_time, row.end_time) for row in rows]
    return weekly.get(day.weekday(), [])


def _local(day: date, moment: time, tz) -> datetime:
    """A wall-clock time on a date in a timezone, as an aware UTC instant (DST gaps resolve forward)."""
    return datetime.combine(day, moment, tzinfo=tz).astimezone(UTC)


def _overlaps(start: datetime, end: datetime, busy: list[tuple[datetime, datetime]]) -> bool:
    return any(start < b_end and b_start < end for b_start, b_end in busy)


def local_busy(range_start: datetime, range_end: datetime, exclude_uid=None) -> list[tuple[datetime, datetime]]:
    """Confirmed bookings of every type near the range, each widened by its own buffers."""
    margin = timedelta(days=1)
    qs = (Booking.objects.filter(status=Booking.Status.CONFIRMED,
                                 start_at__lt=range_end + margin, end_at__gt=range_start - margin)
          .select_related("event_type"))
    if exclude_uid:
        qs = qs.exclude(uid=exclude_uid)
    return [booking.busy_range() for booking in qs]


def compute_slots(event_type: EventType, range_start: datetime, range_end: datetime,
                  busy: list[tuple[datetime, datetime]], now: datetime | None = None) -> list[Slot]:
    """Every offered slot whose start lies in [range_start, range_end). ``busy`` is all blocked time.

    Pure apart from the reads of the type's own rows and its day counts, so it can be tested
    with any busy list.
    """
    now = now or timezone.now()
    tz = event_type.tz
    duration = timedelta(minutes=event_type.duration_minutes)
    step = timedelta(minutes=max(event_type.slot_step_minutes, 1))
    before = timedelta(minutes=event_type.buffer_before_minutes)
    after = timedelta(minutes=event_type.buffer_after_minutes)
    earliest = max(range_start, now + timedelta(minutes=event_type.min_notice_minutes))
    latest = min(range_end, now + timedelta(days=event_type.max_days_ahead))
    if earliest >= latest:
        return []

    weekly: dict[int, list[tuple[time, time]]] = {}
    for window in event_type.windows.all():
        weekly.setdefault(window.weekday, []).append((window.start_time, window.end_time))
    first_day = earliest.astimezone(tz).date() - timedelta(days=1)
    last_day = latest.astimezone(tz).date() + timedelta(days=1)
    overrides: dict[date, list] = {}
    for row in event_type.overrides.filter(date__gte=first_day, date__lte=last_day):
        overrides.setdefault(row.date, []).append(row)

    per_day: dict[date, int] = {}
    if event_type.daily_limit:
        for booking in Booking.objects.filter(event_type=event_type, status=Booking.Status.CONFIRMED,
                                              start_at__gte=_local(first_day, time(0), tz),
                                              start_at__lt=_local(last_day + timedelta(days=1), time(0), tz)):
            key = booking.start_at.astimezone(tz).date()
            per_day[key] = per_day.get(key, 0) + 1

    slots: list[Slot] = []
    day = first_day
    while day <= last_day:
        if event_type.daily_limit and per_day.get(day, 0) >= event_type.daily_limit:
            day += timedelta(days=1)
            continue
        for start_t, end_t in sorted(_windows_for(event_type, day, weekly, overrides)):
            cursor = _local(day, start_t, tz)
            window_end = _local(day, end_t, tz)
            while cursor + duration <= window_end:
                if earliest <= cursor < latest and not _overlaps(cursor - before, cursor + duration + after, busy):
                    slots.append(Slot(cursor, cursor + duration))
                cursor += step
        day += timedelta(days=1)
    return sorted(set(slots), key=lambda slot: slot.start)


def available_slots(event_type: EventType, range_start: datetime, range_end: datetime,
                    fresh: bool = False, exclude_uid=None) -> list[Slot]:
    """Offered slots in a range, with Google busy time and local bookings both applied.

    ``fresh`` skips the free/busy cache: the reservation itself always checks against Google's
    current answer, so two visitors cannot book the same hour from a cached page.
    """
    from . import google

    busy = local_busy(range_start, range_end, exclude_uid=exclude_uid)
    busy += google.busy_ranges(range_start - timedelta(days=1), range_end + timedelta(days=1), fresh=fresh)
    return compute_slots(event_type, range_start, range_end, busy)


def is_available(event_type: EventType, start: datetime) -> bool:
    """Whether one exact start time is offered right now (Google checked fresh)."""
    day_start = start - timedelta(hours=1)
    day_end = start + timedelta(hours=1)
    return any(slot.start == start for slot in available_slots(event_type, day_start, day_end, fresh=True))
