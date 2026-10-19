"""What happens to a booking: creating it, writing it to Google, its emails, cancelling it, and the minute tick.

Views and the admin call these; nothing here knows about HTTP.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from django.db import connection, transaction
from django.utils import timezone

from . import availability, google, mail
from .models import Booking, BookingAttendee, BookingFile, EventType, FollowUp

log = logging.getLogger(__name__)

MAX_ATTEMPTS = 8
# How long the guest's confirmation waits for the Meet link when Google failed at booking time.
MEET_WAIT = timedelta(minutes=15)
# Advisory-lock key that serialises reservations, so two visitors never take one slot.
LOCK_KEY = 0x6B62_6F6F


class SlotTaken(Exception):
    pass


@dataclass
class GuestDetails:
    name: str
    email: str
    timezone: str
    company: str = ""
    phone: str = ""
    socials: list[dict] = field(default_factory=list)
    message: str = ""
    additional: list[str] = field(default_factory=list)
    files: list = field(default_factory=list)


def _lock() -> None:
    if connection.vendor == "postgresql":
        with connection.cursor() as cursor:
            cursor.execute("SELECT pg_advisory_xact_lock(%s)", [LOCK_KEY])


def create_booking(event_type: EventType, start: datetime, guest: GuestDetails) -> Booking:
    """Reserve one slot, then write it to Google and send its emails.

    The slot is checked again inside a lock with Google read fresh; ``SlotTaken`` means it went
    in the meantime. Google and email failures never lose the booking: the worker retries them.
    """
    with transaction.atomic():
        _lock()
        if not availability.is_available(event_type, start):
            raise SlotTaken
        booking = Booking.objects.create(
            event_type=event_type, start_at=start, end_at=start + timedelta(minutes=event_type.duration_minutes),
            guest_name=guest.name, guest_email=guest.email, guest_company=guest.company, guest_phone=guest.phone,
            guest_socials=guest.socials, guest_message=guest.message, guest_timezone=guest.timezone)
        for upload in guest.files:
            BookingFile.objects.create(booking=booking, file=upload, original_name=upload.name[:255],
                                       size=upload.size or 0, content_type=(upload.content_type or "")[:120])
        BookingAttendee.objects.create(booking=booking, email=guest.email, name=guest.name,
                                       role=BookingAttendee.Role.GUEST)
        seen = {guest.email.lower()}
        for email in guest.additional:
            if email.lower() not in seen:
                seen.add(email.lower())
                BookingAttendee.objects.create(booking=booking, email=email, role=BookingAttendee.Role.ADDITIONAL)
        for fixed in event_type.fixed_attendees.all():
            if fixed.email.lower() not in seen:
                seen.add(fixed.email.lower())
                BookingAttendee.objects.create(booking=booking, email=fixed.email, name=fixed.name,
                                               role=BookingAttendee.Role.FIXED, receive_emails=fixed.receive_emails)
    sync_to_google(booking)
    send_new_booking_emails(booking)
    return booking


def sync_to_google(booking: Booking) -> bool:
    """Write the booking to the calendar with its Meet link. True once it is there."""
    if booking.google_event_id:
        return True
    if not google.GoogleAccount.load().is_connected:
        return False
    try:
        event = google.create_event(booking)
    except google.GoogleError as error:
        log.warning("calendar write failed for %s: %s", booking.uid, error)
        Booking.objects.filter(pk=booking.pk).update(sync_attempts=booking.sync_attempts + 1, sync_error=str(error)[:2000])
        booking.sync_attempts += 1
        return False
    booking.google_event_id = event.get("id", "")
    booking.google_event_link = event.get("htmlLink", "")
    booking.meet_url = google.meet_url_of(event)
    booking.sync_error = ""
    booking.save(update_fields=["google_event_id", "google_event_link", "meet_url", "sync_error", "updated_at"])
    return True


def _waiting_for_meet(booking: Booking) -> bool:
    """True while the guest's confirmation should still wait for the calendar write to bring a Meet link."""
    if not booking.event_type.create_meet_link or booking.meet_url:
        return False
    if not google.GoogleAccount.load().is_connected or booking.sync_attempts >= MAX_ATTEMPTS:
        return False
    return timezone.now() - booking.created_at < MEET_WAIT


def send_new_booking_emails(booking: Booking) -> None:
    """The guest's confirmation and the owner's notice, each sent once."""
    if not mail.can_send():
        return
    errors = []
    if not booking.owner_notified_at:
        try:
            mail.notify_owner(booking)
            booking.owner_notified_at = timezone.now()
        except Exception as error:
            errors.append(f"owner: {error}")
    if not booking.confirmation_sent_at and not _waiting_for_meet(booking):
        try:
            mail.send_confirmation(booking)
            booking.confirmation_sent_at = timezone.now()
        except Exception as error:
            errors.append(f"guest: {error}")
    if errors:
        log.warning("booking emails failed for %s: %s", booking.uid, errors)
        booking.mail_attempts += 1
        booking.mail_error = "; ".join(errors)[:2000]
    elif booking.confirmation_sent_at and booking.owner_notified_at:
        booking.mail_error = ""
    booking.save(update_fields=["owner_notified_at", "confirmation_sent_at", "mail_attempts", "mail_error", "updated_at"])


def cancel_booking(booking: Booking, notify: bool = True) -> None:
    if booking.status == Booking.Status.CANCELLED:
        return
    if booking.google_event_id:
        try:
            google.delete_event(booking.google_event_id)
        except google.GoogleError as error:
            log.warning("calendar delete failed for %s: %s", booking.uid, error)
    booking.status = Booking.Status.CANCELLED
    booking.save(update_fields=["status", "updated_at"])
    if notify and booking.start_at > timezone.now():
        mail.send_cancellation(booking)


def tick(now: datetime | None = None) -> dict:
    """One pass of the background work. Safe to run every minute, and twice at once.

    Retries calendar writes and unsent emails, sends the reminders that are due, and sends the
    owner the follow-up reminders that are due.
    """
    now = now or timezone.now()
    done = {"synced": 0, "emails": 0, "reminders": 0, "follow_ups": 0}
    upcoming = Booking.objects.filter(status=Booking.Status.CONFIRMED, end_at__gt=now).select_related("event_type")

    for booking in upcoming.filter(google_event_id="", sync_attempts__lt=MAX_ATTEMPTS):
        if sync_to_google(booking):
            done["synced"] += 1

    pending = upcoming.filter(mail_attempts__lt=MAX_ATTEMPTS).filter(
        confirmation_sent_at__isnull=True) | upcoming.filter(mail_attempts__lt=MAX_ATTEMPTS, owner_notified_at__isnull=True)
    for booking in pending.distinct():
        send_new_booking_emails(booking)
        done["emails"] += 1

    if not mail.can_send():
        return done
    for booking in upcoming.filter(reminder_sent_at__isnull=True, start_at__gt=now):
        if booking.start_at - timedelta(minutes=booking.event_type.reminder_minutes) > now:
            continue
        # Claim the reminder first so a second worker never sends it twice.
        if not Booking.objects.filter(pk=booking.pk, reminder_sent_at__isnull=True).update(reminder_sent_at=now):
            continue
        try:
            mail.send_reminder(booking)
            done["reminders"] += 1
        except Exception:
            log.exception("reminder failed for %s", booking.uid)
            Booking.objects.filter(pk=booking.pk).update(reminder_sent_at=None)

    for follow_up in FollowUp.objects.filter(is_done=False, reminder_sent_at__isnull=True, due_at__lte=now).select_related("booking__event_type"):
        if not FollowUp.objects.filter(pk=follow_up.pk, reminder_sent_at__isnull=True).update(reminder_sent_at=now):
            continue
        try:
            mail.send_follow_up_reminder(follow_up)
            done["follow_ups"] += 1
        except Exception:
            log.exception("follow-up reminder failed for %s", follow_up.pk)
            FollowUp.objects.filter(pk=follow_up.pk).update(reminder_sent_at=None)
    return done
