"""Import meetings already on the connected Google calendar as bookings of one meeting type.

    manage.py booking_import_calendar --since 2026-10-12 --event-type startup [--until 2027-01-01]
        [--exclude-guest other@me.com] [--dry-run]

A meeting is an event with a time (not all-day), not cancelled, with a Meet link or another
attendee, and not already a booking. The guest is the first attendee that is not the account
itself and not an --exclude-guest address; with none, the guest's email stays empty and the
name comes from the title, without a trailing "(Type)". Excluded addresses become fixed
attendees. Imported meetings are marked as already notified and reminded: the import never
emails anyone and never writes to the calendar.
"""

import re
from datetime import datetime, time
from zoneinfo import ZoneInfo

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from keel_booking import google
from keel_booking.models import Booking, BookingAttendee, EventType

TITLE_SUFFIX = re.compile(r"\s*\([^)]*\)\s*$")


class Command(BaseCommand):
    help = "Import existing Google Calendar meetings as bookings of one meeting type, without emailing anyone."

    def add_arguments(self, parser):
        parser.add_argument("--since", required=True, help="First day, YYYY-MM-DD, in the type's timezone.")
        parser.add_argument("--until", help="Day after the last one, YYYY-MM-DD. Default: one year after --since.")
        parser.add_argument("--event-type", required=True, help="Slug of the meeting type to file them under.")
        parser.add_argument("--exclude-guest", action="append", default=[], help="An attendee who is never the guest.")
        parser.add_argument("--dry-run", action="store_true")

    def handle(self, *args, since, until, event_type, exclude_guest, dry_run, **options):
        try:
            et = EventType.objects.get(slug=event_type)
        except EventType.DoesNotExist:
            raise CommandError(f"No meeting type with slug {event_type!r}.")
        account = google.GoogleAccount.load()
        if not account.is_connected:
            raise CommandError("Connect Google first.")
        tz = ZoneInfo(et.timezone)
        start = datetime.combine(datetime.fromisoformat(since).date(), time(0), tzinfo=tz)
        end = (datetime.combine(datetime.fromisoformat(until).date(), time(0), tzinfo=tz) if until
               else start.replace(year=start.year + 1))
        excluded = {email.lower() for email in exclude_guest}
        known = set(Booking.objects.exclude(google_event_id="").values_list("google_event_id", flat=True))

        events, token = [], None
        while True:
            params = {"timeMin": start.isoformat(), "timeMax": end.isoformat(), "singleEvents": "true",
                      "orderBy": "startTime", "maxResults": 250}
            if token:
                params["pageToken"] = token
            page = google._call("GET", f"{google.CALENDAR_API}/calendars/{account.calendar_id}/events", account,
                                params=params).json()
            events += page.get("items", [])
            token = page.get("nextPageToken")
            if not token:
                break

        imported = skipped = 0
        for event in events:
            label = f"{event['start'].get('dateTime') or event['start'].get('date')} {event.get('summary', '(no title)')}"
            others = [a for a in event.get("attendees", []) if not a.get("self")]
            reason = None
            if event.get("status") == "cancelled":
                reason = "cancelled"
            elif "dateTime" not in event["start"]:
                reason = "all-day"
            elif event["id"] in known:
                reason = "already a booking"
            elif not (event.get("hangoutLink") or others):
                reason = "no Meet link and no other attendee"
            if reason:
                skipped += 1
                self.stdout.write(f"skip   {label}: {reason}")
                continue
            guest = next((a for a in others if a.get("email", "").lower() not in excluded), None)
            name = (guest or {}).get("displayName") or TITLE_SUFFIX.sub("", event.get("summary", "")).strip() or "Unknown guest"
            self.stdout.write(f"import {label} -> {et.slug}: {name} <{(guest or {}).get('email', '')}>")
            imported += 1
            if dry_run:
                continue
            created = parse_datetime(event.get("created", "")) or timezone.now()
            with transaction.atomic():
                booking = Booking.objects.create(
                    event_type=et, source=Booking.Source.CALENDAR,
                    start_at=parse_datetime(event["start"]["dateTime"]), end_at=parse_datetime(event["end"]["dateTime"]),
                    guest_name=name[:120], guest_email=(guest or {}).get("email", ""), guest_timezone=et.timezone,
                    guest_message=(event.get("description") or "")[:4000],
                    google_event_id=event["id"], google_event_link=event.get("htmlLink", ""),
                    meet_url=google.meet_url_of(event),
                    confirmation_sent_at=created, owner_notified_at=created, reminder_sent_at=created)
                for attendee in others:
                    email = attendee.get("email", "")
                    role = (BookingAttendee.Role.GUEST if guest and email == guest.get("email")
                            else BookingAttendee.Role.FIXED if email.lower() in excluded
                            else BookingAttendee.Role.ADDITIONAL)
                    BookingAttendee.objects.create(booking=booking, email=email, name=attendee.get("displayName", ""),
                                                   role=role, receive_emails=False)
        verb = "would import" if dry_run else "imported"
        self.stdout.write(self.style.SUCCESS(f"{verb} {imported}, skipped {skipped}"))
