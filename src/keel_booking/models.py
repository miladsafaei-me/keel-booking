"""Booking data: meeting types and their rules, bookings, notes, follow-ups, and the Google link.

Every table is prefixed ``keel_booking_`` so the package sits beside any host app.
Times are stored in UTC; a meeting type's weekly hours are wall-clock times in its own timezone.
"""

from __future__ import annotations

import copy
import uuid
import zoneinfo
from datetime import timedelta

from django.core.exceptions import ValidationError
from django.core.validators import RegexValidator
from django.conf import settings
from django.db import models
from django.utils import timezone, translation
from django.utils.translation import gettext

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
    price = models.CharField(max_length=60, blank=True,
                             help_text="The price as visitors read it, such as Free or $100. Empty shows no price.")
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
    requires_approval = models.BooleanField(
        default=False,
        help_text="A booking is only a request until the owner approves it: the slot is held, the guest is told "
                  "the request arrived, and the Meet link goes out only on approval. The form also asks for "
                  "Telegram or WhatsApp.")
    requires_payment = models.BooleanField(
        default=False,
        help_text="With approval on: approving asks the guest to pay first. The payment email lists the active "
                  "payment methods; the guest replies with the receipt, and the owner's 'payment received' sends "
                  "the confirmation with the Meet link. Unpaid by the deadline, the time is released.")

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

    def journey(self) -> list[dict]:
        """What happens after the form, step by step, for a type that requires approval: each step's title and
        one line of what the guest does or gets, in the active language. Empty for a type booked at once."""
        if not self.requires_approval:
            return []
        steps = [
            {"key": "request", "title": gettext("You send the request"),
             "text": gettext("Pick a time and fill in the form. The time is held for you.")},
            {"key": "review", "title": gettext("I review it"),
             "text": gettext("I answer by email as soon as I can.")},
        ]
        if self.requires_payment:
            steps.append({"key": "payment", "title": gettext("You pay"),
                          "text": gettext("The email has the ways to pay. Reply to it with the receipt.")})
        steps.append({"key": "meet", "title": gettext("You get the Google Meet link"),
                      "text": gettext("The confirmation email has the link and the calendar invitation.")})
        return steps

    @property
    def tz(self) -> zoneinfo.ZoneInfo:
        return zoneinfo.ZoneInfo(self.timezone)

    def highlight_list(self) -> list[str]:
        return [line.strip() for line in self.highlights.splitlines() if line.strip()]

    def localized(self, language: str | None = None) -> "EventType":
        """A copy whose page and email texts are in ``language`` (the active one by default).

        Each text comes from the type's translation in that language when it has one, and stays
        as written on the type otherwise. The copy is for display only; never save it.
        """
        language = (language or translation.get_language() or "").split("-")[0]
        rows = getattr(self, "_prefetched_objects_cache", {}).get("translations")
        row = (next((r for r in rows if r.language == language), None) if rows is not None
               else self.translations.filter(language=language).first())
        if row is None:
            return self
        local = copy.copy(self)
        for name in EventTypeTranslation.TEXT_FIELDS:
            value = getattr(row, name)
            if value:
                setattr(local, name, value)
        return local


def language_choices():
    return [(code, name) for code, name in settings.LANGUAGES]


class EventTypeTranslation(models.Model):
    """A meeting type's page and email texts in one more language. An empty text keeps the type's own."""

    TEXT_FIELDS = ("name", "tagline", "description", "highlights", "price", "confirmation_note")

    event_type = models.ForeignKey(EventType, on_delete=models.CASCADE, related_name="translations")
    language = models.CharField(max_length=10, choices=language_choices)
    name = models.CharField(max_length=120, blank=True)
    tagline = models.CharField(max_length=200, blank=True)
    description = models.TextField(blank=True)
    highlights = models.TextField(blank=True, help_text="One point per line.")
    price = models.CharField(max_length=60, blank=True)
    confirmation_note = models.TextField(blank=True)

    class Meta:
        db_table = "keel_booking_event_type_translation"
        unique_together = ("event_type", "language")
        ordering = ("language",)
        verbose_name = "translation"

    def __str__(self) -> str:
        return f"{self.event_type.slug} ({self.language})"


class EventQuestion(models.Model):
    """One more question on a meeting type's form: a short or long text, a list to pick from, or boxes to tick.

    Answers are stored on the booking under the question's ``key``; for a choice question the
    stored value is the option's key, so renaming an option's label never breaks old answers.
    """

    class Kind(models.TextChoices):
        TEXT = "text", "Short text"
        TEXTAREA = "textarea", "Long text"
        SELECT = "select", "Dropdown, one answer"
        RADIO = "radio", "Options, one answer"
        CHECKBOX = "checkbox", "Boxes, several answers"

    CHOICE_KINDS = (Kind.SELECT, Kind.RADIO, Kind.CHECKBOX)

    event_type = models.ForeignKey(EventType, on_delete=models.CASCADE, related_name="questions")
    key = models.SlugField(max_length=40, validators=[slug_validator],
                           help_text="A stable name; the answers are stored under it. Never change it once used.")
    label = models.CharField(max_length=200)
    help_text = models.CharField(max_length=300, blank=True, help_text="A short line under the question.")
    kind = models.CharField(max_length=12, choices=Kind.choices, default=Kind.TEXT)
    options = models.TextField(blank=True, help_text="For a dropdown, options or boxes: one per line, as key: label.")
    required = models.BooleanField(default=False)
    sort_order = models.PositiveIntegerField(default=0)

    class Meta:
        db_table = "keel_booking_event_question"
        unique_together = ("event_type", "key")
        ordering = ("sort_order", "id")
        verbose_name = "form question"

    def __str__(self) -> str:
        return f"{self.event_type.slug}: {self.label}"

    @property
    def field_name(self) -> str:
        return f"q_{self.key}"

    @property
    def is_choice(self) -> bool:
        return self.kind in self.CHOICE_KINDS

    def option_list(self) -> list[tuple[str, str]]:
        """The options as (key, label), in order. A line without a colon is its own key and label."""
        rows = []
        for line in self.options.splitlines():
            if not line.strip():
                continue
            key, sep, label = line.partition(":")
            rows.append((key.strip(), label.strip()) if sep else (line.strip(), line.strip()))
        return rows

    def clean(self) -> None:
        if self.is_choice:
            keys = [key for key, _label in self.option_list()]
            if len(keys) < 2:
                raise ValidationError({"options": "A choice question needs at least two options."})
            if len(set(keys)) != len(keys):
                raise ValidationError({"options": "Two options share one key."})

    def localized(self, language: str | None = None) -> "EventQuestion":
        """A copy with the label, hint and option labels in ``language``; a missing text keeps the original."""
        language = (language or translation.get_language() or "").split("-")[0]
        rows = getattr(self, "_prefetched_objects_cache", {}).get("translations")
        row = (next((r for r in rows if r.language == language), None) if rows is not None
               else self.translations.filter(language=language).first())
        if row is None:
            return self
        local = copy.copy(self)
        local.label = row.label or self.label
        local.help_text = row.help_text or self.help_text
        labels = [line.strip() for line in row.options.splitlines() if line.strip()]
        options = self.option_list()
        if labels and len(labels) == len(options):
            local.options = "\n".join(f"{key}: {label}" for (key, _old), label in zip(options, labels))
        return local

    def answer_text(self, value) -> str:
        """A stored answer as the reader sees it: option keys turned into their labels."""
        if not self.is_choice:
            return str(value)
        labels = dict(self.option_list())
        values = value if isinstance(value, list) else [value]
        return ", ".join(labels.get(v, v) for v in values)


class EventQuestionTranslation(models.Model):
    """A question's texts in one more language. Options are labels only, one per line, in the question's order."""

    question = models.ForeignKey(EventQuestion, on_delete=models.CASCADE, related_name="translations")
    language = models.CharField(max_length=10, choices=language_choices)
    label = models.CharField(max_length=200, blank=True)
    help_text = models.CharField(max_length=300, blank=True)
    options = models.TextField(blank=True, help_text="One label per line, in the same order as the question's options.")

    class Meta:
        db_table = "keel_booking_event_question_translation"
        unique_together = ("question", "language")
        ordering = ("language",)
        verbose_name = "translation"

    def __str__(self) -> str:
        return f"{self.question.key} ({self.language})"


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


def phone_digits(value: str) -> str:
    """A phone number as digits only, its country code first: «+90 555 123 4567» is 905551234567."""
    digits = "".join(ch for ch in value if ch.isdigit())
    return digits[2:] if digits.startswith("00") else digits


def looks_like_phone(value: str) -> bool:
    text = value.strip()
    return bool(text) and all(ch.isdigit() or ch in "+-() ." for ch in text) and len(phone_digits(text)) >= 7


def telegram_link(value: str) -> str:
    """A t.me link that opens a chat: by username (@name, name or a t.me link) or by phone number."""
    text = value.strip()
    if looks_like_phone(text):
        return f"https://t.me/+{phone_digits(text)}"
    for prefix in ("https://", "http://", "www.", "t.me/", "telegram.me/"):
        if text.lower().startswith(prefix):
            text = text[len(prefix):]
    return "https://t.me/" + text.lstrip("@").split("/")[0].split("?")[0]


def whatsapp_link(value: str) -> str:
    return f"https://wa.me/{phone_digits(value)}"


class Booking(models.Model):
    class Status(models.TextChoices):
        PENDING = "pending", "Awaiting approval"
        PAYMENT = "payment", "Approved, awaiting payment"
        CONFIRMED = "confirmed", "Confirmed"
        DECLINED = "declined", "Declined"
        CANCELLED = "cancelled", "Cancelled"

    # The statuses whose time is taken: a request waiting for the owner holds its slot.
    HOLDING = (Status.PENDING, Status.PAYMENT, Status.CONFIRMED)

    uid = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    event_type = models.ForeignKey(EventType, on_delete=models.PROTECT, related_name="bookings")
    status = models.CharField(max_length=12, choices=Status.choices, default=Status.CONFIRMED, db_index=True)
    start_at = models.DateTimeField(db_index=True)
    end_at = models.DateTimeField()

    class Source(models.TextChoices):
        SITE = "site", "Booked on the site"
        CALENDAR = "calendar", "Imported from Google Calendar"

    source = models.CharField(max_length=12, choices=Source.choices, default=Source.SITE, db_index=True)
    guest_name = models.CharField(max_length=120)
    guest_email = models.EmailField(blank=True, help_text="Empty only on a meeting imported without the guest's email.")
    guest_company = models.CharField("brand or company", max_length=160, blank=True)
    guest_phone = models.CharField("phone", max_length=40, blank=True)
    guest_socials = models.JSONField("social profiles", default=list, blank=True,
                                     help_text="A list of {network, handle} the guest gave.")
    guest_message = models.TextField("description", blank=True)
    guest_telegram = models.CharField("Telegram", max_length=80, blank=True, help_text="A Telegram ID or number.")
    guest_whatsapp = models.CharField("WhatsApp", max_length=40, blank=True, help_text="A WhatsApp number.")
    answers = models.JSONField(default=dict, blank=True,
                               help_text="The answers to the meeting type's own questions, by question key.")
    guest_timezone = models.CharField(max_length=64, default="UTC", validators=[validate_timezone])
    language = models.CharField(max_length=10, blank=True,
                                help_text="The language the guest booked in; their emails are written in it.")

    google_event_id = models.CharField(max_length=255, blank=True)
    google_event_link = models.URLField(max_length=500, blank=True)
    meet_url = models.URLField(max_length=300, blank=True)
    meet_access = models.CharField(max_length=12, blank=True, help_text="The Meet access type the site last set.")
    sync_attempts = models.PositiveSmallIntegerField(default=0)
    sync_error = models.TextField(blank=True)

    decided_at = models.DateTimeField(null=True, blank=True, help_text="When the owner approved or declined the request.")
    request_ack_sent_at = models.DateTimeField(null=True, blank=True, help_text="When the guest was told the request arrived.")
    decision_sent_at = models.DateTimeField(null=True, blank=True, help_text="When the guest was told the request was declined.")
    payment_due_at = models.DateTimeField(null=True, blank=True, help_text="The guest must pay by then, or the time is released.")
    payment_sent_at = models.DateTimeField(null=True, blank=True, help_text="When the payment details went to the guest.")
    paid_at = models.DateTimeField(null=True, blank=True, help_text="When the owner marked the payment received.")
    telegram_message_id = models.BigIntegerField(null=True, blank=True, help_text="The owner's Telegram message about this request.")

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

    def journey(self) -> list[dict]:
        """The type's steps (``EventType.journey``), each marked done, now or next for this booking."""
        steps = self.event_type.localized().journey()
        current = {self.Status.PENDING: "review", self.Status.PAYMENT: "payment"}.get(self.status)
        keys = [step["key"] for step in steps]
        at = keys.index(current) if current in keys else (len(keys) if self.status == self.Status.CONFIRMED else -1)
        for index, step in enumerate(steps):
            step["number"] = index + 1
            step["state"] = "done" if index < at else ("now" if index == at else "next")
        return steps if at >= 0 else []

    @property
    def is_request(self) -> bool:
        """True while the booking is not yet a confirmed meeting: waiting for approval, or for the payment."""
        return self.status in (self.Status.PENDING, self.Status.PAYMENT)

    def busy_range(self):
        """The time this booking blocks, its type's buffers included."""
        et = self.event_type
        return (self.start_at - timedelta(minutes=et.buffer_before_minutes),
                self.end_at + timedelta(minutes=et.buffer_after_minutes))

    @property
    def guest_language(self) -> str:
        """The language of the guest-side emails: the one booked in, else the site's default."""
        return self.language or settings.LANGUAGE_CODE.split("-")[0]

    def language_name(self) -> str:
        """The booked language named in the active language: Persian, Farsça, فارسی."""
        try:
            return str(translation.get_language_info(self.guest_language)["name_translated"])
        except KeyError:
            return self.guest_language

    def messenger_links(self) -> list[tuple[str, str, str]]:
        """The guest's Telegram and WhatsApp as (network, what they typed, a link that opens a chat with them)."""
        rows = []
        if self.guest_telegram:
            rows.append(("Telegram", self.guest_telegram, telegram_link(self.guest_telegram)))
        if self.guest_whatsapp:
            rows.append(("WhatsApp", self.guest_whatsapp, whatsapp_link(self.guest_whatsapp)))
        return rows

    def socials_display(self) -> list[tuple[str, str]]:
        """The guest's social profiles as (network name, handle)."""
        names = dict(conf.get("SOCIAL_NETWORKS"))
        return [(names.get(item.get("network"), str(item.get("network", "")).capitalize()), item.get("handle", ""))
                for item in self.guest_socials or [] if isinstance(item, dict)]

    def answers_display(self, language: str | None = None) -> list[tuple[str, str]]:
        """The answers as (question, answer) in ``language`` (the active one by default), in the form's order.

        An answer whose question was since removed still shows, under its key.
        """
        answers = dict(self.answers or {})
        rows = []
        for question in self.event_type.questions.prefetch_related("translations"):
            if question.key in answers:
                local = question.localized(language)
                rows.append((local.label, local.answer_text(answers.pop(question.key))))
        rows += [(key, ", ".join(value) if isinstance(value, list) else str(value)) for key, value in answers.items()]
        return rows

    def email_recipients(self) -> list[str]:
        """Everyone who gets the guest-side emails: the guest, the guests they added, and opted-in fixed attendees."""
        emails = [self.guest_email]
        emails += [a.email for a in self.attendees.all() if a.role == BookingAttendee.Role.ADDITIONAL]
        emails += [a.email for a in self.attendees.all() if a.role == BookingAttendee.Role.FIXED and a.receive_emails]
        return list(dict.fromkeys(e.lower() for e in emails if e))


class PaymentMethod(models.Model):
    """One way to pay for a paid meeting, shown as a card in the payment email.

    ``amount_text`` may use {price} (the meeting's price as written in the email's language) and
    {amount} (the number in the type's own price, such as 100 for $100). ``details`` is one
    "Label: value" per line; each value is shown large and easy to copy.
    """

    name = models.CharField(max_length=120, help_text="The card's title, such as Bank transfer in lira.")
    amount_text = models.CharField(max_length=200, blank=True, help_text="Such as: The equivalent of {price} in lira.")
    details = models.TextField(help_text="One 'Label: value' per line, such as IBAN: TR00 0000 ...")
    note = models.CharField(max_length=300, blank=True, help_text="A short line under the card.")
    is_active = models.BooleanField(default=True)
    sort_order = models.PositiveIntegerField(default=0)

    class Meta:
        db_table = "keel_booking_payment_method"
        ordering = ("sort_order", "id")
        verbose_name = "payment method"

    def __str__(self) -> str:
        return self.name

    def localized(self, language: str | None = None) -> "PaymentMethod":
        language = (language or translation.get_language() or "").split("-")[0]
        rows = getattr(self, "_prefetched_objects_cache", {}).get("translations")
        row = (next((r for r in rows if r.language == language), None) if rows is not None
               else self.translations.filter(language=language).first())
        if row is None:
            return self
        local = copy.copy(self)
        for name in ("name", "amount_text", "details", "note"):
            value = getattr(row, name)
            if value:
                setattr(local, name, value)
        return local

    def detail_rows(self) -> list[tuple[str, str]]:
        rows = []
        for line in self.details.splitlines():
            if line.strip():
                label, sep, value = line.partition(":")
                rows.append((label.strip(), value.strip()) if sep else ("", line.strip()))
        return rows

    def amount_for(self, event_type) -> str:
        """The amount line for one meeting type, in the active language."""
        digits = "".join(ch for ch in event_type.price if ch.isdigit() or ch == ".")
        local_price = event_type.localized().price or event_type.price
        try:
            return self.amount_text.format(price=local_price, amount=digits)
        except (KeyError, IndexError, ValueError):
            return self.amount_text


class PaymentMethodTranslation(models.Model):
    """A payment method's texts in one more language. An empty text keeps the method's own."""

    method = models.ForeignKey(PaymentMethod, on_delete=models.CASCADE, related_name="translations")
    language = models.CharField(max_length=10, choices=language_choices)
    name = models.CharField(max_length=120, blank=True)
    amount_text = models.CharField(max_length=200, blank=True)
    details = models.TextField(blank=True, help_text="One 'Label: value' per line; empty keeps the method's own.")
    note = models.CharField(max_length=300, blank=True)

    class Meta:
        db_table = "keel_booking_payment_method_translation"
        unique_together = ("method", "language")
        ordering = ("language",)
        verbose_name = "translation"

    def __str__(self) -> str:
        return f"{self.method} ({self.language})"


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
