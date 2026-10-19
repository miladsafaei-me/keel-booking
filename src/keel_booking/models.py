"""Booking data: meeting types and their rules, bookings, notes, follow-ups, and the Google link.

Every table is prefixed ``keel_booking_`` so the package sits beside any host app.
Times are stored in UTC; a meeting type's weekly hours are wall-clock times in its own timezone.
"""

from __future__ import annotations

import uuid
import zoneinfo
from datetime import timedelta

from django.core.exceptions import ValidationError
from django.core.validators import RegexValidator
from django.db import models
from django.utils import timezone

from . import conf

slug_validator = RegexValidator(r"^[a-z0-9]+(?:-[a-z0-9]+)*$", "Lowercase letters, digits and single hyphens only.")


def validate_timezone(value: str) -> None:
    try:
        zoneinfo.ZoneInfo(value)
    except (zoneinfo.ZoneInfoNotFoundError, ValueError):
        raise ValidationError(f"{value!r} is not an IANA timezone, such as Europe/Istanbul.")


def default_timezone() -> str:
    return conf.get("OWNER_TIMEZONE")


class Weekday(models.IntegerChoices):
    MONDAY = 0, "Monday"
    TUESDAY = 1, "Tuesday"
    WEDNESDAY = 2, "Wednesday"
    THURSDAY = 3, "Thursday"
    FRIDAY = 4, "Friday"
    SATURDAY = 5, "Saturday"
    SUNDAY = 6, "Sunday"


class GoogleAccount(models.Model):
    """The one Google account whose calendar is checked and written, and whose Gmail can send the emails."""

    email = models.EmailField(blank=True)
    refresh_token = models.TextField(blank=True)
    access_token = models.TextField(blank=True)
    token_expiry = models.DateTimeField(null=True, blank=True)
    scopes = models.TextField(blank=True)
    calendar_id = models.CharField(
        max_length=255, default="primary",
        help_text="The calendar new meetings are written to. 'primary' is the account's main calendar.")
    busy_calendar_ids = models.TextField(
        blank=True,
        help_text="Other calendars whose events also block slots, one calendar ID per line. "
                  "The calendar above is always checked.")
    connected_at = models.DateTimeField(null=True, blank=True)
    last_error = models.TextField(blank=True)

    class Meta:
        db_table = "keel_booking_google_account"
        verbose_name = "Google account"
        verbose_name_plural = "Google account"

    def __str__(self) -> str:
        return self.email or "Not connected"

    @classmethod
    def load(cls) -> "GoogleAccount":
        account, _ = cls.objects.get_or_create(pk=1)
        return account

    @property
    def is_connected(self) -> bool:
        return bool(self.refresh_token)

    def calendars_to_check(self) -> list[str]:
        extra = [line.strip() for line in self.busy_calendar_ids.splitlines() if line.strip()]
        return list(dict.fromkeys([self.calendar_id, *extra]))


class EventType(models.Model):
    """One kind of meeting with its own page, hours and rules."""

    slug = models.SlugField(max_length=60, unique=True, validators=[slug_validator],
                            help_text="The page address: /book/<slug>/.")
    name = models.CharField(max_length=120)
    tagline = models.CharField(max_length=200, blank=True, help_text="One line for the list of meeting types.")
    description = models.TextField(blank=True, help_text="The paragraph at the top of the meeting's page.")
    highlights = models.TextField(blank=True, help_text="What the meeting covers, one point per line.")
    icon = models.CharField(max_length=40, blank=True, help_text="An icon name from the site's icon set.")
    is_active = models.BooleanField(default=True, help_text="Inactive types disappear from the list and cannot be booked.")
    sort_order = models.PositiveIntegerField(default=0)

    duration_minutes = models.PositiveIntegerField(default=30)
    buffer_before_minutes = models.PositiveIntegerField(default=0, help_text="Free time kept before each meeting.")
    buffer_after_minutes = models.PositiveIntegerField(default=0, help_text="Free time kept after each meeting.")
    slot_step_minutes = models.PositiveIntegerField(default=30, help_text="How often a start time is offered.")
    min_notice_minutes = models.PositiveIntegerField(default=240, help_text="The earliest a slot can start, from now.")
    max_days_ahead = models.PositiveIntegerField(default=30, help_text="How far ahead a visitor can book.")
    daily_limit = models.PositiveIntegerField(default=0, help_text="Most meetings of this type per day; 0 means no limit.")
    timezone = models.CharField(max_length=64, default=default_timezone, validators=[validate_timezone],
                                help_text="The timezone of the weekly hours below, and the page's default.")
    detect_visitor_timezone = models.BooleanField(
        default=True, help_text="Show times in the visitor's own timezone, read from their browser.")
    reminder_minutes = models.PositiveIntegerField(default=30, help_text="Minutes before the start that the reminder email goes out.")
    create_meet_link = models.BooleanField(default=True, help_text="Create a Google Meet link for every meeting.")
    allow_additional_guests = models.BooleanField(default=True, help_text="Let the visitor invite other people by email.")
    confirmation_note = models.TextField(blank=True, help_text="An extra paragraph in the confirmation email.")

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "keel_booking_event_type"
        ordering = ("sort_order", "name")
        verbose_name = "meeting type"

    def __str__(self) -> str:
        return self.name

    def clean(self) -> None:
        if self.slug in conf.get("RESERVED_SLUGS"):
            raise ValidationError({"slug": f"'{self.slug}' is reserved by the booking pages."})
        if not self.duration_minutes:
            raise ValidationError({"duration_minutes": "A meeting needs a length."})
        if not self.slot_step_minutes:
            raise ValidationError({"slot_step_minutes": "The step must be at least one minute."})

    @property
    def tz(self) -> zoneinfo.ZoneInfo:
        return zoneinfo.ZoneInfo(self.timezone)

    def highlight_list(self) -> list[str]:
        return [line.strip() for line in self.highlights.splitlines() if line.strip()]


class AvailabilityWindow(models.Model):
    """Weekly hours: a weekday and a time range. A weekday with no window is a day off."""

    event_type = models.ForeignKey(EventType, on_delete=models.CASCADE, related_name="windows")
    weekday = models.PositiveSmallIntegerField(choices=Weekday.choices)
    start_time = models.TimeField()
    end_time = models.TimeField()

    class Meta:
        db_table = "keel_booking_availability_window"
        ordering = ("weekday", "start_time")
        verbose_name = "weekly hours"
        verbose_name_plural = "weekly hours"

    def __str__(self) -> str:
        return f"{self.get_weekday_display()} {self.start_time:%H:%M}-{self.end_time:%H:%M}"

    def clean(self) -> None:
        if self.start_time and self.end_time and self.end_time <= self.start_time:
            raise ValidationError("The end time must be after the start time.")


class DateOverride(models.Model):
    """One date that differs from the weekly hours: either a day off, or its own time range.

    Several ranges on one date add up; any 'unavailable' row on a date closes the whole day.
    """

    event_type = models.ForeignKey(EventType, on_delete=models.CASCADE, related_name="overrides")
    date = models.DateField()
    is_unavailable = models.BooleanField(default=True, help_text="Closed all day. Untick and set times to open other hours.")
    start_time = models.TimeField(null=True, blank=True)
    end_time = models.TimeField(null=True, blank=True)

    class Meta:
        db_table = "keel_booking_date_override"
        ordering = ("date", "start_time")
        verbose_name = "date override"

    def __str__(self) -> str:
        if self.is_unavailable:
            return f"{self.date} closed"
        return f"{self.date} {self.start_time:%H:%M}-{self.end_time:%H:%M}"

    def clean(self) -> None:
        if not self.is_unavailable:
            if not (self.start_time and self.end_time):
                raise ValidationError("An open date needs a start and an end time.")
            if self.end_time <= self.start_time:
                raise ValidationError("The end time must be after the start time.")


class FixedAttendee(models.Model):
    """A person (usually a Google account) invited to every meeting of one type."""

    event_type = models.ForeignKey(EventType, on_delete=models.CASCADE, related_name="fixed_attendees")
    email = models.EmailField()
    name = models.CharField(max_length=120, blank=True)
    receive_emails = models.BooleanField(default=True, help_text="Also send this person the confirmation and reminder emails.")

    class Meta:
        db_table = "keel_booking_fixed_attendee"
        unique_together = ("event_type", "email")
        verbose_name = "fixed attendee"

    def __str__(self) -> str:
        return self.email


class Booking(models.Model):
    class Status(models.TextChoices):
        CONFIRMED = "confirmed", "Confirmed"
        CANCELLED = "cancelled", "Cancelled"

    uid = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    event_type = models.ForeignKey(EventType, on_delete=models.PROTECT, related_name="bookings")
    status = models.CharField(max_length=12, choices=Status.choices, default=Status.CONFIRMED, db_index=True)
    start_at = models.DateTimeField(db_index=True)
    end_at = models.DateTimeField()

    guest_name = models.CharField(max_length=120)
    guest_email = models.EmailField()
    guest_company = models.CharField("brand or company", max_length=160, blank=True)
    guest_phone = models.CharField("phone", max_length=40, blank=True)
    guest_socials = models.JSONField("social profiles", default=list, blank=True,
                                     help_text="A list of {network, handle} the guest gave.")
    guest_message = models.TextField("description", blank=True)
    guest_timezone = models.CharField(max_length=64, default="UTC", validators=[validate_timezone])

    google_event_id = models.CharField(max_length=255, blank=True)
    google_event_link = models.URLField(max_length=500, blank=True)
    meet_url = models.URLField(max_length=300, blank=True)
    sync_attempts = models.PositiveSmallIntegerField(default=0)
    sync_error = models.TextField(blank=True)

    confirmation_sent_at = models.DateTimeField(null=True, blank=True)
    owner_notified_at = models.DateTimeField(null=True, blank=True)
    reminder_sent_at = models.DateTimeField(null=True, blank=True)
    mail_attempts = models.PositiveSmallIntegerField(default=0)
    mail_error = models.TextField(blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "keel_booking_booking"
        ordering = ("-start_at",)

    def __str__(self) -> str:
        return f"{self.event_type} with {self.guest_name}, {timezone.localtime(self.start_at):%Y-%m-%d %H:%M}"

    @property
    def is_upcoming(self) -> bool:
        return self.status == self.Status.CONFIRMED and self.end_at > timezone.now()

    def busy_range(self):
        """The time this booking blocks, its type's buffers included."""
        et = self.event_type
        return (self.start_at - timedelta(minutes=et.buffer_before_minutes),
                self.end_at + timedelta(minutes=et.buffer_after_minutes))

    def email_recipients(self) -> list[str]:
        """Everyone who gets the guest-side emails: the guest, the guests they added, and opted-in fixed attendees."""
        emails = [self.guest_email]
        emails += [a.email for a in self.attendees.all() if a.role == BookingAttendee.Role.ADDITIONAL]
        emails += [a.email for a in self.attendees.all() if a.role == BookingAttendee.Role.FIXED and a.receive_emails]
        return list(dict.fromkeys(e.lower() for e in emails))


class BookingAttendee(models.Model):
    class Role(models.TextChoices):
        GUEST = "guest", "Guest"
        ADDITIONAL = "additional", "Added by the guest"
        FIXED = "fixed", "Fixed attendee"

    booking = models.ForeignKey(Booking, on_delete=models.CASCADE, related_name="attendees")
    email = models.EmailField()
    name = models.CharField(max_length=120, blank=True)
    role = models.CharField(max_length=12, choices=Role.choices)
    receive_emails = models.BooleanField(default=True)

    class Meta:
        db_table = "keel_booking_booking_attendee"
        verbose_name = "attendee"

    def __str__(self) -> str:
        return self.email


def booking_file_path(instance, filename: str) -> str:
    return f"keel_booking/{instance.booking.uid}/{filename}"


class BookingFile(models.Model):
    """A file the guest attached to the booking. Private: served only to staff through the admin."""

    booking = models.ForeignKey(Booking, on_delete=models.CASCADE, related_name="files")
    file = models.FileField(upload_to=booking_file_path, max_length=300)
    original_name = models.CharField(max_length=255)
    size = models.PositiveIntegerField(default=0)
    content_type = models.CharField(max_length=120, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "keel_booking_booking_file"
        verbose_name = "file"

    def __str__(self) -> str:
        return self.original_name


class BookingNote(models.Model):
    """The owner's private note on a meeting."""

    booking = models.ForeignKey(Booking, on_delete=models.CASCADE, related_name="notes")
    body = models.TextField()
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "keel_booking_booking_note"
        ordering = ("created_at",)
        verbose_name = "note"

    def __str__(self) -> str:
        return self.body[:60]


class FollowUp(models.Model):
    """Something the owner must do after a meeting, with a reminder email at its due time."""

    booking = models.ForeignKey(Booking, on_delete=models.CASCADE, related_name="follow_ups")
    due_at = models.DateTimeField(db_index=True)
    note = models.TextField(blank=True, help_text="What to do, shown in the reminder.")
    is_done = models.BooleanField(default=False)
    reminder_sent_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "keel_booking_follow_up"
        ordering = ("due_at",)
        verbose_name = "follow-up"

    def __str__(self) -> str:
        return f"Follow up {self.booking.guest_name} on {timezone.localtime(self.due_at):%Y-%m-%d %H:%M}"
