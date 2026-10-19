"""The slot engine, the reservation flow and the tick, with Google left unconnected."""

import json
import tempfile
from datetime import datetime, time, timedelta, timezone as dt_timezone
from unittest import mock
from zoneinfo import ZoneInfo

from django.core import mail as outbox
from django.contrib.auth.models import User
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings

from keel_booking import availability, services
from keel_booking.models import (AvailabilityWindow, Booking, DateOverride, EventType, FixedAttendee, FollowUp,
                                 BookingAttendee)

UTC = dt_timezone.utc
IST = ZoneInfo("Europe/Istanbul")
# Monday 2026-11-02, 06:00 UTC (09:00 in Istanbul).
NOW = datetime(2026, 11, 2, 6, 0, tzinfo=UTC)


def make_type(**fields):
    defaults = dict(slug="seo", name="SEO consultation", duration_minutes=45, slot_step_minutes=30,
                    min_notice_minutes=0, max_days_ahead=30, timezone="Europe/Istanbul")
    defaults.update(fields)
    et = EventType.objects.create(**defaults)
    for weekday in range(5):
        AvailabilityWindow.objects.create(event_type=et, weekday=weekday, start_time=time(10), end_time=time(12))
    return et


def ist(day, hour, minute=0):
    return datetime(2026, 11, day, hour, minute, tzinfo=IST).astimezone(UTC)


class SlotEngineTests(TestCase):
    def slots(self, et, busy=(), start=None, end=None):
        start = start or ist(2, 0)
        end = end or ist(3, 0)
        return [s.start for s in availability.compute_slots(et, start, end, list(busy), now=NOW)]

    def test_weekly_hours_in_type_timezone(self):
        et = make_type()
        self.assertEqual(self.slots(et), [ist(2, 10), ist(2, 10, 30), ist(2, 11)])

    def test_weekend_is_closed(self):
        et = make_type()
        self.assertEqual(self.slots(et, start=ist(7, 0), end=ist(9, 0)), [])

    def test_busy_and_buffers_remove_slots(self):
        et = make_type(buffer_after_minutes=15)
        busy = [(ist(2, 11), ist(2, 11, 30))]
        # 10:00 ends 10:45 + 15 buffer = 11:00, touching only; 10:30 and 11:00 overlap.
        self.assertEqual(self.slots(et, busy), [ist(2, 10)])

    def test_min_notice_and_window(self):
        et = make_type(min_notice_minutes=90)
        self.assertEqual(self.slots(et), [ist(2, 10, 30), ist(2, 11)])
        et.max_days_ahead = 0
        self.assertEqual(self.slots(et), [])

    def test_override_closes_and_opens(self):
        et = make_type()
        DateOverride.objects.create(event_type=et, date=datetime(2026, 11, 2).date(), is_unavailable=True)
        self.assertEqual(self.slots(et), [])
        DateOverride.objects.create(event_type=et, date=datetime(2026, 11, 7).date(), is_unavailable=False,
                                    start_time=time(14), end_time=time(15))
        self.assertEqual(self.slots(et, start=ist(7, 0), end=ist(8, 0)), [ist(7, 14)])

    def test_daily_limit(self):
        et = make_type(daily_limit=1)
        Booking.objects.create(event_type=et, start_at=ist(2, 10), end_at=ist(2, 10, 45), guest_name="A",
                               guest_email="a@example.com")
        self.assertEqual(self.slots(et), [])


@mock.patch("keel_booking.availability.timezone.now", return_value=NOW)
class ReserveTests(TestCase):
    def setUp(self):
        self.et = make_type()
        FixedAttendee.objects.create(event_type=self.et, email="partner@example.com")

    def post(self, **data):
        body = {"start": "2026-11-02T07:00:00Z", "name": "Sara", "email": "sara@example.com",
                "timezone": "Europe/Berlin", "guests": "colleague@example.com", **data}
        return self.client.post("/book/seo/reserve/", json.dumps(body), content_type="application/json")

    def test_reserve_creates_booking_attendees_and_emails(self, _now):
        response = self.post()
        self.assertEqual(response.status_code, 201, response.content)
        booking = Booking.objects.get()
        self.assertEqual(booking.start_at, ist(2, 10))
        roles = sorted(booking.attendees.values_list("role", flat=True))
        self.assertEqual(roles, ["additional", "fixed", "guest"])
        recipients = sorted(m.to[0] for m in outbox.outbox)
        self.assertEqual(recipients, ["colleague@example.com", "owner@example.com", "partner@example.com", "sara@example.com"])
        self.assertIsNotNone(booking.confirmation_sent_at)
        self.assertIn("text/calendar", outbox.outbox[0].attachments[0][2])
        page = self.client.get(response.json()["redirect"])
        self.assertContains(page, "You are booked")

    @override_settings(EMAIL_BACKEND="keel_booking.mail.GmailBackend")
    def test_emails_wait_for_google_without_using_retries(self, _now):
        self.assertEqual(self.post().status_code, 201)
        booking = Booking.objects.get()
        self.assertEqual((booking.mail_attempts, booking.confirmation_sent_at, len(outbox.outbox)), (0, None, 0))
        services.tick()
        booking.refresh_from_db()
        self.assertEqual(booking.mail_attempts, 0)

    def test_slot_taken_twice(self, _now):
        self.assertEqual(self.post().status_code, 201)
        self.assertEqual(self.post(email="other@example.com").status_code, 409)

    def test_validation(self, _now):
        response = self.post(email="nope", start="2026-11-02T07:10:00Z")
        self.assertEqual(response.status_code, 422)
        self.assertIn("email", response.json()["fields"])

    def test_off_grid_time_is_refused(self, _now):
        self.assertEqual(self.post(start="2026-11-02T07:10:00Z").status_code, 409)

    @override_settings(MEDIA_ROOT=tempfile.mkdtemp())
    def test_multipart_with_files_socials_and_phone(self, _now):
        brief = SimpleUploadedFile("brief.pdf", b"%PDF-1.4 test", content_type="application/pdf")
        response = self.client.post("/book/seo/reserve/", {
            "start": "2026-11-02T07:00:00Z", "timezone": "Europe/Berlin", "name": "Sara", "company": "Acme",
            "email": "sara@example.com", "phone": "+90 555 000 0000", "message": "Our SEO plan",
            "social_network": ["linkedin", "telegram"], "social_handle": ["sara-acme", ""], "files": [brief]})
        self.assertEqual(response.status_code, 201, response.content)
        booking = Booking.objects.get()
        self.assertEqual(booking.guest_socials, [{"network": "linkedin", "handle": "sara-acme"}])
        self.assertEqual(booking.guest_phone, "+90 555 000 0000")
        item = booking.files.get()
        self.assertEqual(item.original_name, "brief.pdf")
        owner_mail = next(m for m in outbox.outbox if m.to == ["owner@example.com"])
        self.assertIn("brief.pdf", [a[0] for a in owner_mail.attachments])
        url = f"/book/admin/keel_booking/booking/file/{item.pk}/"
        self.assertEqual(self.client.get(url).status_code, 302)
        User.objects.create_superuser("owner", "owner@example.com", "pw")
        self.client.login(username="owner", password="pw")
        self.assertEqual(b"".join(self.client.get(url).streaming_content), b"%PDF-1.4 test")
        listing = self.client.get("/book/admin/keel_booking/booking/")
        self.assertContains(listing, "Acme")
        self.assertContains(listing, "LinkedIn: sara-acme")

    def test_bad_file_type_is_refused(self, _now):
        bad = SimpleUploadedFile("run.exe", b"MZ", content_type="application/octet-stream")
        response = self.client.post("/book/seo/reserve/", {"start": "2026-11-02T07:00:00Z", "name": "Sara",
                                                           "email": "sara@example.com", "files": [bad]})
        self.assertEqual(response.status_code, 422)
        self.assertIn("files", response.json()["fields"])

    def test_slots_endpoint(self, _now):
        data = self.client.get("/book/seo/slots/?month=2026-11&tz=Europe/Istanbul").json()
        self.assertEqual(data["days"]["2026-11-02"], ["2026-11-02T07:00:00Z", "2026-11-02T07:30:00Z", "2026-11-02T08:00:00Z"])


class GmailBackendTests(TestCase):
    def test_sends_a_raw_message_with_attachment(self):
        from django.core.mail import EmailMultiAlternatives
        from keel_booking.mail import GmailBackend

        message = EmailMultiAlternatives("Hi", "text", "owner@example.com", ["guest@example.com"])
        message.attach_alternative("<p>html</p>", "text/html")
        message.attach("invite.ics", "BEGIN:VCALENDAR", "text/calendar")
        with mock.patch("keel_booking.google.send_raw_email") as send:
            self.assertEqual(GmailBackend().send_messages([message]), 1)
        raw = send.call_args[0][0]
        self.assertIn(b"To: guest@example.com", raw)
        self.assertIn(b"invite.ics", raw)


class ImportCalendarTests(TestCase):
    def test_imports_meetings_once_and_emails_nobody(self):
        from django.core.management import call_command
        from keel_booking.models import GoogleAccount

        make_type(slug="startup", name="Startup consultation")
        GoogleAccount.objects.create(pk=1, refresh_token="t", email="owner@example.com")
        events = {"items": [
            {"id": "e1", "summary": "Masoud Seifi (Startup)", "status": "confirmed", "htmlLink": "https://cal/e1",
             "hangoutLink": "https://meet.google.com/aaa-bbbb-ccc", "created": "2026-10-10T10:00:00Z",
             "start": {"dateTime": "2026-10-12T17:30:00+03:00"}, "end": {"dateTime": "2026-10-12T18:15:00+03:00"},
             "attendees": [{"email": "second@example.com"}, {"email": "owner@example.com", "self": True}]},
            {"id": "e2", "summary": "Happy birthday!", "status": "confirmed", "start": {"date": "2027-06-19"}, "end": {"date": "2027-06-20"}},
        ]}
        with mock.patch("keel_booking.google._call") as call:
            call.return_value.json.return_value = events
            call_command("booking_import_calendar", "--since", "2026-10-12", "--event-type", "startup",
                         "--exclude-guest", "second@example.com", stdout=open("/dev/null", "w"))
            call_command("booking_import_calendar", "--since", "2026-10-12", "--event-type", "startup",
                         "--exclude-guest", "second@example.com", stdout=open("/dev/null", "w"))
        booking = Booking.objects.get()
        self.assertEqual((booking.guest_name, booking.guest_email, booking.source), ("Masoud Seifi", "", "calendar"))
        self.assertEqual(booking.meet_url, "https://meet.google.com/aaa-bbbb-ccc")
        self.assertEqual(list(booking.attendees.values_list("email", "role")), [("second@example.com", "fixed")])
        self.assertEqual(services.tick(now=NOW)["emails"], 0)
        self.assertEqual(len(outbox.outbox), 0)


class MeetAccessTests(TestCase):
    def setUp(self):
        from keel_booking.models import GoogleAccount

        self.account = GoogleAccount.objects.create(
            pk=1, refresh_token="t", access_token="a", token_expiry=NOW + timedelta(days=3650), email="owner@example.com",
            scopes="openid https://www.googleapis.com/auth/meetings.space.settings")
        self.et = make_type(slug="startup", name="Startup consultation")
        self.booking = Booking.objects.create(
            event_type=self.et, start_at=NOW + timedelta(days=2), end_at=NOW + timedelta(days=2, minutes=45),
            guest_name="Masoud", guest_email="", google_event_id="ev1", meet_url="https://meet.google.com/abc-defg-hij",
            source="calendar", confirmation_sent_at=NOW, owner_notified_at=NOW, reminder_sent_at=NOW)
        BookingAttendee.objects.create(booking=self.booking, email="second@example.com", role="fixed", receive_emails=False)

    def test_guest_email_added_later_reaches_the_event(self):
        self.booking.guest_email = "masoud@example.com"
        self.booking.save()
        services.ensure_guest_attendee(self.booking)
        current = {"attendees": [{"email": "owner@example.com", "self": True, "organizer": True, "responseStatus": "accepted"},
                                 {"email": "second@example.com", "responseStatus": "accepted"}]}
        with mock.patch("keel_booking.google._call") as call:
            call.return_value.json.return_value = current
            self.assertTrue(services.push_attendees(self.booking))
        patch = call.call_args_list[-1]
        self.assertEqual(patch.args[0], "PATCH")
        sent = patch.kwargs["json"]["attendees"]
        self.assertEqual([a["email"] for a in sent], ["owner@example.com", "second@example.com", "masoud@example.com"])
        self.assertEqual(sent[1]["responseStatus"], "accepted")
        self.assertEqual(len(outbox.outbox), 0)

    @override_settings(KEEL_BOOKING={"OWNER_NAME": "Test Owner", "OWNER_EMAIL": "owner@example.com", "MEET_ACCESS": "OPEN"})
    def test_meet_access_open_is_set_once_by_the_tick(self):
        with mock.patch("keel_booking.google._call") as call:
            call.return_value.json.return_value = {"name": "spaces/xYz123"}
            services.tick(now=NOW)
            services.tick(now=NOW)
        meet_calls = [c for c in call.call_args_list if "meet.googleapis.com" in c.args[1]]
        self.assertEqual([(c.args[0], c.args[1]) for c in meet_calls],
                         [("GET", "https://meet.googleapis.com/v2/spaces/abc-defg-hij"),
                          ("PATCH", "https://meet.googleapis.com/v2/spaces/xYz123")])
        self.assertEqual(meet_calls[1].kwargs["json"], {"config": {"accessType": "OPEN"}})
        self.booking.refresh_from_db()
        self.assertEqual(self.booking.meet_access, "OPEN")


class TickTests(TestCase):
    def test_reminder_and_follow_up(self):
        et = make_type()
        booking = Booking.objects.create(event_type=et, start_at=NOW + timedelta(minutes=20),
                                         end_at=NOW + timedelta(minutes=65), guest_name="Sara",
                                         guest_email="sara@example.com", guest_timezone="Europe/Berlin",
                                         confirmation_sent_at=NOW, owner_notified_at=NOW)
        BookingAttendee.objects.create(booking=booking, email="sara@example.com", role="guest")
        FollowUp.objects.create(booking=booking, due_at=NOW - timedelta(minutes=1), note="Send the proposal")
        done = services.tick(now=NOW)
        self.assertEqual(done["reminders"], 1)
        self.assertEqual(done["follow_ups"], 1)
        self.assertEqual(sorted(m.to[0] for m in outbox.outbox), ["owner@example.com", "owner@example.com", "sara@example.com"])
        self.assertEqual(services.tick(now=NOW), {"synced": 0, "emails": 0, "reminders": 0, "follow_ups": 0})
