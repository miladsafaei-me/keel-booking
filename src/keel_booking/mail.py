"""Booking emails: rendering, the calendar attachment, and a Django email backend that sends through Gmail.

Every email is an HTML template plus a plain-text one under ``keel_booking/email/``; a host
overrides either by shipping a template of the same name. Colours and fonts come from
``KEEL_BOOKING["EMAIL_THEME"]`` because email clients ignore CSS variables.

The guest-side emails, the calendar file and the Google Calendar link are written in the language
the guest booked in (``Booking.language``); the owner's emails stay in English.
"""

from __future__ import annotations

import logging
import zoneinfo
from datetime import datetime, timezone as dt_timezone
from urllib.parse import urlencode

from django.core.mail import EmailMultiAlternatives
from django.core.mail.backends.base import BaseEmailBackend
from django.template.loader import render_to_string
from django.urls import reverse
from django.utils import translation
from django.utils.translation import gettext as _

from . import conf, i18n

log = logging.getLogger(__name__)


class GmailBackend(BaseEmailBackend):
    """Sends Django email through the Gmail API of the connected Google account.

    Set ``EMAIL_BACKEND = "keel_booking.mail.GmailBackend"``. The From address must be that
    account or one of its verified aliases.
    """

    def send_messages(self, email_messages):
        from . import google

        sent = 0
        for message in email_messages:
            try:
                google.send_raw_email(message.message().as_bytes())
                sent += 1
            except google.GoogleError:
                if not self.fail_silently:
                    raise
                log.exception("Gmail send failed")
        return sent


def can_send() -> bool:
    """False while the email backend is Gmail and no Google account is connected yet.

    Emails then wait, without using up their retries, and go out on the first tick after the
    owner connects Google.
    """
    from django.conf import settings

    from .models import GoogleAccount

    if settings.EMAIL_BACKEND.endswith("GmailBackend"):
        return GoogleAccount.load().is_connected
    return True


def _fmt_offset(moment: datetime) -> str:
    offset = moment.utcoffset()
    minutes = int(offset.total_seconds() // 60) if offset else 0
    sign = "+" if minutes >= 0 else "-"
    hours, rest = divmod(abs(minutes), 60)
    return f"GMT{sign}{hours}" + (f":{rest:02d}" if rest else "")


def when(booking, tz_name: str) -> dict:
    """The meeting's time as words, in one timezone and the active language."""
    tz = zoneinfo.ZoneInfo(tz_name)
    start = booking.start_at.astimezone(tz)
    end = booking.end_at.astimezone(tz)
    date, hours = i18n.long_date(start), i18n.time_range(start, end)
    return {
        "date": date,
        "time": hours,
        "full": _("{date}, {time}").format(date=date, time=hours),
        "timezone": f"{tz_name.replace('_', ' ')} ({_fmt_offset(start)})",
        "short": i18n.short_date(start),
    }


def _stamp(moment: datetime) -> str:
    return moment.astimezone(dt_timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _ics_escape(text: str) -> str:
    return text.replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,").replace("\n", "\\n")


def _title(booking) -> str:
    """The meeting's name for calendars, in the active language: «SEO consultation with Jane Doe»."""
    return _("{meeting} with {owner}").format(meeting=booking.event_type.localized().name, owner=conf.get("OWNER_NAME"))


def _details(booking) -> str:
    text = _title(booking) + "."
    if booking.meet_url:
        text += "\n" + _("Join: {url}").format(url=booking.meet_url)
    return text


def ics(booking) -> str:
    """A one-event calendar file for the attachment (PUBLISH: it adds the meeting, it asks for no reply)."""
    domain = conf.get("SITE_URL").split("//")[-1] or "keel-booking"
    title = _title(booking)
    description = _details(booking)
    lines = [
        "BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//Keel//Booking//EN", "CALSCALE:GREGORIAN", "METHOD:PUBLISH",
        "BEGIN:VEVENT",
        f"UID:{booking.uid}@{domain}",
        f"DTSTAMP:{_stamp(booking.created_at)}",
        f"DTSTART:{_stamp(booking.start_at)}",
        f"DTEND:{_stamp(booking.end_at)}",
        f"SUMMARY:{_ics_escape(title)}",
        f"DESCRIPTION:{_ics_escape(description)}",
    ]
    if booking.meet_url:
        lines += [f"LOCATION:{_ics_escape(booking.meet_url)}", f"URL:{booking.meet_url}"]
    lines += [f"ORGANIZER;CN={_ics_escape(conf.get('OWNER_NAME'))}:mailto:{conf.get('OWNER_EMAIL')}",
              "STATUS:CONFIRMED", "END:VEVENT", "END:VCALENDAR"]
    return "\r\n".join(lines) + "\r\n"


def google_calendar_link(booking) -> str:
    query = {"action": "TEMPLATE", "text": _title(booking),
             "dates": f"{_stamp(booking.start_at)}/{_stamp(booking.end_at)}", "details": _details(booking)}
    if booking.meet_url:
        query["location"] = booking.meet_url
    return "https://calendar.google.com/calendar/render?" + urlencode(query)


def _theme() -> dict:
    """The email theme for the active language: a right-to-left language takes its own font."""
    theme = conf.get("EMAIL_THEME")
    if i18n.is_rtl():
        theme = {**theme, "font_heading": theme["font_rtl"], "font_body": theme["font_rtl"]}
    return theme


def _context(booking, tz_name: str, **extra) -> dict:
    rtl = i18n.is_rtl()
    return {
        "booking": booking,
        "event_type": booking.event_type.localized(),
        "when": when(booking, tz_name),
        "owner_name": conf.get("OWNER_NAME"),
        "owner_email": conf.get("OWNER_EMAIL"),
        "theme": _theme(),
        "rtl": rtl,
        "language": i18n.base(),
        # Small labels are spaced capitals in Latin script; Arabic script has neither, and spacing breaks its letters.
        "caps": "" if rtl else "letter-spacing:1px;text-transform:uppercase;",
        "caps_wide": "" if rtl else "letter-spacing:2px;text-transform:uppercase;",
        "start": "right" if rtl else "left",
        "end": "left" if rtl else "right",
        "site_url": conf.get("SITE_URL"),
        "page_url": conf.absolute(reverse("keel_booking:confirmed", args=[booking.uid])),
        "book_url": conf.absolute(reverse("keel_booking:event_type", args=[booking.event_type.slug])),
        "calendar_link": google_calendar_link(booking),
        **extra,
    }


def _send(template: str, subject: str, to: list[str], context: dict, attach_ics=None, reply_to=None,
          files=()) -> None:
    text = render_to_string(f"keel_booking/email/{template}.txt", context)
    html = render_to_string(f"keel_booking/email/{template}.html", {**context, "subject": subject})
    for address in to:
        message = EmailMultiAlternatives(subject, text, conf.from_email(), [address], reply_to=reply_to)
        message.attach_alternative(html, "text/html")
        if attach_ics:
            message.attach("invite.ics", attach_ics, "text/calendar; charset=utf-8; method=PUBLISH")
        for item in files:
            with item.file.open("rb") as handle:
                message.attach(item.original_name, handle.read(), item.content_type or "application/octet-stream")
        message.send()


def _owner_language():
    """The owner's emails are written in English, whatever the page the guest booked on."""
    return translation.override("en")


def send_confirmation(booking) -> None:
    with translation.override(booking.guest_language):
        context = _context(booking, booking.guest_timezone)
        subject = _("Confirmed: {meeting} with {owner}, {when}").format(
            meeting=context["event_type"].name, owner=conf.get("OWNER_NAME"), when=context["when"]["short"])
        _send("confirmation", subject, booking.email_recipients(), context, attach_ics=ics(booking),
              reply_to=[conf.get("OWNER_EMAIL")])


def notify_owner(booking) -> None:
    files = list(booking.files.all())
    attach = files if sum(f.size for f in files) <= conf.get("OWNER_ATTACH_MB") * 1024 * 1024 else []
    with _owner_language():
        context = _context(booking, booking.event_type.timezone, files=files, files_attached=bool(attach),
                           file_links=[(f, conf.absolute(reverse("admin:keel_booking_booking_file", args=[f.pk]))) for f in files],
                           admin_url=conf.absolute(reverse("admin:keel_booking_booking_change", args=[booking.pk])))
        subject = f"New booking: {booking.event_type.name} with {booking.guest_name}, {context['when']['short']}"
        _send("owner_new_booking", subject, [conf.get("OWNER_EMAIL")], context, attach_ics=ics(booking),
              reply_to=[booking.guest_email] if booking.guest_email else None, files=attach)


def send_reminder(booking) -> None:
    minutes = booking.event_type.reminder_minutes
    with translation.override(booking.guest_language):
        guest_context = _context(booking, booking.guest_timezone, minutes=i18n.digits(minutes))
        subject = _("Starts in {minutes} minutes: {meeting} with {owner}").format(
            minutes=i18n.digits(minutes), meeting=guest_context["event_type"].name, owner=conf.get("OWNER_NAME"))
        _send("reminder", subject, booking.email_recipients(), guest_context, reply_to=[conf.get("OWNER_EMAIL")])
    with _owner_language():
        owner_context = _context(booking, booking.event_type.timezone, minutes=minutes, for_owner=True,
                                 admin_url=conf.absolute(reverse("admin:keel_booking_booking_change", args=[booking.pk])))
        _send("reminder", f"Starts in {minutes} minutes: {booking.event_type.name} with {booking.guest_name}",
              [conf.get("OWNER_EMAIL")], owner_context, reply_to=[booking.guest_email] if booking.guest_email else None)


def send_cancellation(booking) -> None:
    with translation.override(booking.guest_language):
        context = _context(booking, booking.guest_timezone)
        subject = _("Cancelled: {meeting} with {owner}, {when}").format(
            meeting=context["event_type"].name, owner=conf.get("OWNER_NAME"), when=context["when"]["short"])
        _send("cancellation", subject, booking.email_recipients(), context, reply_to=[conf.get("OWNER_EMAIL")])


def send_follow_up_reminder(follow_up) -> None:
    booking = follow_up.booking
    tz = zoneinfo.ZoneInfo(booking.event_type.timezone)
    with _owner_language():
        context = _context(booking, booking.event_type.timezone, follow_up=follow_up,
                           due=f"{follow_up.due_at.astimezone(tz):%A %d %B, %H:%M}",
                           admin_url=conf.absolute(reverse("admin:keel_booking_booking_change", args=[booking.pk])),
                           notes=list(booking.notes.all()))
        _send("follow_up", f"Follow up: {booking.guest_name} ({booking.event_type.name})",
              [conf.get("OWNER_EMAIL")], context, reply_to=[booking.guest_email] if booking.guest_email else None)
