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

from keel_booking import availability, mail, services
from keel_booking.models import (AvailabilityWindow, Booking, DateOverride, EventQuestion, EventQuestionTranslation,
                                 EventType, EventTypeTranslation, FixedAttendee, FollowUp, BookingAttendee)

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

    def test_check_returns_the_problems_and_books_nothing(self, _now):
        response = self.post(check="1", email="not-an-email")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(list(response.json()["fields"]), ["email"])
        self.assertEqual(response.json()["fields"]["email"], "Please enter a valid email address, such as name@company.com.")
        self.assertEqual(self.post(check="1", name="").json()["fields"]["name"], "Please enter your name.")
        self.assertEqual(self.post(check="1").json(), {"fields": {}})
        self.assertFalse(Booking.objects.exists())
        self.assertEqual(outbox.outbox, [])

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

    def test_a_failing_guest_address_never_blocks_the_fixed_attendee(self, _now):
        from django.core.mail.backends.locmem import EmailBackend

        real = EmailBackend.send_messages

        def flaky(backend, messages):
            if any("sara@example.com" in m.to for m in messages):
                raise OSError("mailbox unavailable")
            return real(backend, messages)

        with mock.patch.object(EmailBackend, "send_messages", flaky):
            self.assertEqual(self.post().status_code, 201)
        booking = Booking.objects.get()
        sent = sorted(m.to[0] for m in outbox.outbox)
        self.assertEqual(sent, ["colleague@example.com", "owner@example.com", "partner@example.com"])
        self.assertIsNone(booking.confirmation_sent_at)
        self.assertEqual(booking.mail_attempts, 1)
        # The retry reaches only the address that failed: nobody gets a second copy.
        outbox.outbox.clear()
        services.tick()
        booking.refresh_from_db()
        self.assertEqual([m.to for m in outbox.outbox], [["sara@example.com"]])
        self.assertIsNotNone(booking.confirmation_sent_at)

    def test_fixed_attendee_added_later_still_gets_the_confirmation(self, _now):
        FixedAttendee.objects.all().delete()
        with mock.patch("keel_booking.services.mail.send_confirmation", side_effect=OSError("down")):
            self.assertEqual(self.post().status_code, 201)
        FixedAttendee.objects.create(event_type=self.et, email="partner@example.com")
        outbox.outbox.clear()
        services.tick()
        self.assertIn(["partner@example.com"], [m.to for m in outbox.outbox])

    def test_reminder_and_cancellation_reach_the_fixed_attendee(self, _now):
        self.assertEqual(self.post().status_code, 201)
        booking = Booking.objects.get()
        outbox.outbox.clear()
        services.tick(now=booking.start_at - timedelta(minutes=booking.event_type.reminder_minutes - 1))
        self.assertIn(["partner@example.com"], [m.to for m in outbox.outbox])
        outbox.outbox.clear()
        services.cancel_booking(Booking.objects.get())
        self.assertEqual(sorted(m.to[0] for m in outbox.outbox),
                         ["colleague@example.com", "partner@example.com", "sara@example.com"])

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


@mock.patch("keel_booking.availability.timezone.now", return_value=NOW)
class QuestionTests(TestCase):
    """A meeting type's own questions: shown on its page, checked, stored by key and shown to the owner."""

    def setUp(self):
        self.et = make_type()
        stage = EventQuestion.objects.create(event_type=self.et, key="stage", label="Stage", kind="radio",
                                             options="idea: Just an idea\nrunning: Running business", required=True,
                                             sort_order=1)
        EventQuestion.objects.create(event_type=self.et, key="goals", label="Goals", kind="checkbox",
                                     options="growth: Grow sales\ncost: Save time\nproduct: Build a product",
                                     required=True, sort_order=2)
        EventQuestion.objects.create(event_type=self.et, key="team", label="Team size", kind="select",
                                     options="solo: Just me\nsmall: 2 to 10", sort_order=3)
        EventQuestionTranslation.objects.create(question=stage, language="fa", label="مرحله",
                                                options="فقط ایده\nکسب‌وکار فعال")

    def post(self, data):
        body = {"start": "2026-11-02T07:00:00Z", "timezone": "Europe/Berlin", "name": "Sara",
                "email": "sara@example.com", **data}
        return self.client.post("/book/seo/reserve/", body)

    def test_page_shows_the_questions_in_the_active_language(self, _now):
        self.assertContains(self.client.get("/book/seo/"), 'name="q_goals" value="cost"')
        page = self.client.get("/fa/book/seo/")
        self.assertContains(page, "مرحله")
        self.assertContains(page, "کسب‌وکار فعال")

    def test_required_questions_and_unknown_options_are_refused(self, _now):
        response = self.post({"q_stage": "bogus"})
        self.assertEqual(response.status_code, 422)
        self.assertEqual(set(response.json()["fields"]), {"q_stage", "q_goals"})

    def test_answers_are_stored_by_key_and_shown_to_the_owner(self, _now):
        response = self.post({"q_stage": "running", "q_goals": ["product", "growth", "nope"], "q_team": ""})
        self.assertEqual(response.status_code, 201, response.content)
        booking = Booking.objects.get()
        self.assertEqual(booking.answers, {"stage": "running", "goals": ["growth", "product"]})
        self.assertEqual(booking.answers_display(), [("Stage", "Running business"), ("Goals", "Grow sales, Build a product")])
        self.assertEqual(booking.answers_display("fa")[0], ("مرحله", "کسب‌وکار فعال"))
        owner = next(m for m in outbox.outbox if m.to == ["owner@example.com"])
        self.assertIn("Goals: Grow sales, Build a product", owner.body)


@mock.patch("keel_booking.availability.timezone.now", return_value=NOW)
class LanguageTests(TestCase):
    """A booking made on a translated page keeps its language: its pages and the guest's emails follow it."""

    def setUp(self):
        self.et = make_type()
        EventTypeTranslation.objects.create(event_type=self.et, language="fa", name="مشاوره‌ی سئو",
                                            highlights="یک\nدو", price="۱۰۰ دلار")

    def reserve(self, prefix):
        body = {"start": "2026-11-02T07:00:00Z", "name": "Sara", "email": "sara@example.com", "timezone": "Asia/Tehran"}
        return self.client.post(f"{prefix}/book/seo/reserve/", json.dumps(body), content_type="application/json")

    def test_persian_booking(self, _now):
        page = self.client.get("/fa/book/seo/")
        self.assertContains(page, "مشاوره‌ی سئو")
        self.assertContains(page, '"weekStart": 6')
        self.assertContains(page, "۴۵")
        response = self.reserve("/fa")
        self.assertEqual(response.status_code, 201, response.content)
        self.assertTrue(response.json()["redirect"].startswith("/fa/book/confirmed/"))
        booking = Booking.objects.get()
        self.assertEqual(booking.language, "fa")
        guest = next(m for m in outbox.outbox if m.to == ["sara@example.com"])
        owner = next(m for m in outbox.outbox if m.to == ["owner@example.com"])
        self.assertTrue(guest.subject.startswith("تأیید شد: مشاوره‌ی سئو"), guest.subject)
        self.assertIn("دوشنبه، ۲ نوامبر ۲۰۲۶", guest.body)
        self.assertIn("https://example.com/fa/book/confirmed/", guest.body)
        html = guest.alternatives[0][0]
        self.assertIn('dir="rtl"', html)
        self.assertIn("SUMMARY:مشاوره‌ی سئو با Test Owner", guest.attachments[0][1])
        self.assertTrue(owner.subject.startswith("New booking: SEO consultation"), owner.subject)
        self.assertIn("Booked in Persian", owner.body)
        confirmed = self.client.get(response.json()["redirect"])
        self.assertContains(confirmed, "جلسه‌ی شما رزرو شد")

    def test_owner_side_follows_the_booking_language_when_asked(self, _now):
        from django.conf import settings
        from keel_booking import google

        with override_settings(KEEL_BOOKING={**settings.KEEL_BOOKING, "OWNER_EMAILS_IN_BOOKING_LANGUAGE": True}):
            self.assertEqual(self.reserve("/fa").status_code, 201)
            booking = Booking.objects.get()
            owner = next(m for m in outbox.outbox if m.to == ["owner@example.com"])
            self.assertTrue(owner.subject.startswith("رزرو تازه: مشاوره‌ی سئو با Sara"), owner.subject)
            self.assertIn("زبان رزرو: فارسی", owner.body)
            self.assertIn('dir="rtl"', owner.alternatives[0][0])
            body = google.event_body(booking)
            self.assertEqual(body["summary"], "مشاوره‌ی سئو: Sara")
            self.assertIn("زبان: فارسی", body["description"])
            outbox.outbox.clear()
            mail.send_reminder(booking)
            self.assertTrue(all("دقیقه‌ی دیگر" in m.subject for m in outbox.outbox), [m.subject for m in outbox.outbox])

    def test_turkish_errors_and_default_english(self, _now):
        response = self.client.post("/tr/book/seo/reserve/", json.dumps({"start": "2026-11-02T07:00:00Z"}),
                                    content_type="application/json")
        self.assertEqual(response.json()["fields"]["email"], "Lütfen e-posta adresinizi yazın.")
        self.assertEqual(self.reserve("").status_code, 201)
        self.assertEqual(Booking.objects.get().language, "en")
        self.assertContains(self.client.get("/book/seo/"), "SEO consultation")
        self.assertContains(self.client.get("/book/seo/"), '"weekStart": 1')

    def test_translation_falls_back_per_text(self, _now):
        local = self.et.localized("fa")
        self.assertEqual((local.name, local.highlight_list(), local.tagline), ("مشاوره‌ی سئو", ["یک", "دو"], self.et.tagline))
        self.assertEqual(self.et.localized("tr").name, "SEO consultation")
        self.et.price = "$100"
        self.assertEqual((self.et.localized("fa").price, self.et.localized("tr").price), ("۱۰۰ دلار", "$100"))
        self.assertEqual(EventType.objects.get().name, "SEO consultation")


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


@mock.patch("keel_booking.availability.timezone.now", return_value=NOW)
@override_settings(KEEL_BOOKING={"OWNER_NAME": "Test Owner", "OWNER_EMAIL": "owner@example.com",
                                 "OWNER_TIMEZONE": "Europe/Istanbul", "SITE_URL": "https://example.com",
                                 "TELEGRAM_BOT_TOKEN": "123:abc", "TELEGRAM_CHAT_ID": "42"})
class ApprovalTests(TestCase):
    """A type that requires approval: a held request, the owner's Telegram buttons, then the decision emails."""

    def setUp(self):
        from django.core.cache import cache

        cache.clear()
        self.addCleanup(cache.clear)
        self.et = make_type(slug="intro", name="Intro call", requires_approval=True)
        self.calls = []

        def fake_call(method, http_timeout=15, **data):
            self.calls.append((method, data))
            return {"message_id": 7} if method == "sendMessage" else True

        patcher = mock.patch("keel_booking.telegram.call", side_effect=fake_call)
        patcher.start()
        self.addCleanup(patcher.stop)

    def post(self, **data):
        body = {"start": "2026-11-02T07:00:00Z", "name": "Sara", "email": "sara@example.com",
                "timezone": "Europe/Berlin", "telegram": "@sara_k", **data}
        return self.client.post("/book/intro/reserve/", json.dumps(body), content_type="application/json")

    def press(self, action, booking, chat=42):
        from keel_booking import telegram

        telegram.handle_update({"update_id": 1, "callback_query": {
            "id": "q1", "data": f"kb:{action}:{booking.uid}", "message": {"message_id": 7, "chat": {"id": chat}}}})

    def test_telegram_or_whatsapp_is_required(self, _now):
        fields = self.post(check="1", telegram="").json()["fields"]
        self.assertEqual(list(fields), ["messenger"])
        self.assertEqual(self.post(check="1", telegram="", whatsapp="+90 555 123 4567").json(), {"fields": {}})
        self.assertIn("whatsapp", self.post(check="1", whatsapp="call me").json()["fields"])
        self.assertIn("telegram", self.post(check="1", telegram="a b").json()["fields"])

    def test_request_is_held_and_announced_without_a_link(self, _now):
        response = self.post(whatsapp="+90 555 123 4567")
        self.assertEqual(response.status_code, 201, response.content)
        booking = Booking.objects.get()
        self.assertEqual(booking.status, Booking.Status.PENDING)
        self.assertEqual(booking.telegram_message_id, 7)
        self.assertEqual([m.to for m in outbox.outbox], [["sara@example.com"]])
        receipt = outbox.outbox[0]
        self.assertIn("I have received your request to set up a meeting", receipt.body)
        self.assertNotIn("meet.google", receipt.body + receipt.alternatives[0][0])
        self.assertEqual(receipt.attachments, [])
        method, data = self.calls[0]
        self.assertEqual(method, "sendMessage")
        self.assertIn("Intro call", data["text"])
        self.assertIn('href="https://t.me/sara_k"', data["text"])
        self.assertIn('href="https://wa.me/905551234567"', data["text"])
        keyboard = data["reply_markup"]["inline_keyboard"]
        self.assertEqual([len(row) for row in keyboard[:2]], [1, 2])
        self.assertIn("kb:reschedule:", keyboard[1][0]["callback_data"])
        self.assertIn("kb:decline:", keyboard[1][1]["callback_data"])
        # The held slot is gone for the next visitor.
        self.assertEqual(self.post(email="other@example.com").status_code, 409)
        self.assertContains(self.client.get(response.json()["redirect"]), "Request received")

    def test_approve_confirms_and_sends_the_link(self, _now):
        self.post()
        booking = Booking.objects.get()
        outbox.outbox.clear()
        booking.meet_url = "https://meet.google.com/abc-defg-hij"
        booking.save()
        self.press("approve", booking)
        booking.refresh_from_db()
        self.assertEqual(booking.status, Booking.Status.CONFIRMED)
        self.assertIsNotNone(booking.confirmation_sent_at)
        guest = [m for m in outbox.outbox if m.to == ["sara@example.com"]]
        self.assertEqual(len(guest), 1)
        self.assertIn("https://meet.google.com/abc-defg-hij", guest[0].body)
        # The owner also gets the email notice with the calendar file, not only the Telegram message.
        owner = [m for m in outbox.outbox if m.to == ["owner@example.com"]]
        self.assertEqual(len(owner), 1)
        self.assertIsNotNone(booking.owner_notified_at)
        edit = [data for method, data in self.calls if method == "editMessageText"][-1]
        self.assertIn("Approved", edit["text"])
        self.assertNotIn("callback_data", json.dumps(edit["reply_markup"]))
        # A second press changes nothing.
        self.press("decline", booking)
        booking.refresh_from_db()
        self.assertEqual(booking.status, Booking.Status.CONFIRMED)

    def test_decline_frees_the_slot_and_tells_the_guest(self, _now):
        self.post()
        booking = Booking.objects.get()
        outbox.outbox.clear()
        self.press("decline", booking)
        booking.refresh_from_db()
        self.assertEqual(booking.status, Booking.Status.DECLINED)
        self.assertEqual([m.to for m in outbox.outbox], [["sara@example.com"]])
        self.assertIn("cannot hold this meeting", outbox.outbox[0].body)
        self.assertEqual(booking.decline_reason, Booking.DeclineReason.CANCEL)
        self.assertEqual(self.post(email="other@example.com").status_code, 201)

    def test_reschedule_frees_the_slot_and_invites_a_new_time(self, _now):
        self.post()
        booking = Booking.objects.get()
        outbox.outbox.clear()
        self.press("reschedule", booking)
        booking.refresh_from_db()
        self.assertEqual(booking.status, Booking.Status.DECLINED)
        self.assertEqual(booking.decline_reason, Booking.DeclineReason.RESCHEDULE)
        self.assertEqual([m.to for m in outbox.outbox], [["sara@example.com"]])
        self.assertIn("cannot be there at the time you picked", outbox.outbox[0].body)
        self.assertIn(f"/{booking.event_type.slug}", outbox.outbox[0].body)
        self.assertNotIn("cannot hold this meeting", outbox.outbox[0].body)
        edit = [data for method, data in self.calls if method == "editMessageText"][-1]
        self.assertIn("pick a new time", edit["text"])
        self.assertNotIn("callback_data", json.dumps(edit["reply_markup"]))
        self.assertEqual(self.post(email="other@example.com").status_code, 201)

    def test_presses_from_another_chat_are_ignored(self, _now):
        self.post()
        booking = Booking.objects.get()
        self.press("approve", booking, chat=99)
        booking.refresh_from_db()
        self.assertEqual(booking.status, Booking.Status.PENDING)

    def test_poll_hands_presses_to_the_handler(self, _now):
        from keel_booking import telegram

        self.post()
        mock.patch.stopall()  # the real call(), over a fake HTTP layer
        booking = Booking.objects.get()
        update = {"update_id": 5, "callback_query": {"id": "q", "data": f"kb:approve:{booking.uid}",
                                                     "message": {"message_id": 7, "chat": {"id": 42}}}}
        with mock.patch("keel_booking.telegram.requests.post") as post:
            post.return_value.json.return_value = {"ok": True, "result": [update]}
            self.assertEqual(telegram.poll(None, timeout=1), 6)
            self.assertEqual(post.call_args_list[0].kwargs["json"]["timeout"], 1)

    def test_undecided_request_lapses_at_its_start(self, _now):
        self.post()
        booking = Booking.objects.get()
        services.tick(now=booking.start_at + timedelta(minutes=1))
        booking.refresh_from_db()
        self.assertEqual(booking.status, Booking.Status.DECLINED)
        self.assertIsNotNone(booking.decision_sent_at)


@mock.patch("keel_booking.availability.timezone.now", return_value=NOW)
@override_settings(KEEL_BOOKING={"OWNER_NAME": "Test Owner", "OWNER_EMAIL": "owner@example.com",
                                 "OWNER_TIMEZONE": "Europe/Istanbul", "SITE_URL": "https://example.com",
                                 "TELEGRAM_BOT_TOKEN": "123:abc", "TELEGRAM_CHAT_ID": "42"})
class PaymentTests(TestCase):
    """A paid type: approval asks for the payment; the owner's 'payment received' sends the Meet link."""

    def setUp(self):
        from django.core.cache import cache
        from keel_booking.models import PaymentMethod

        cache.clear()
        self.addCleanup(cache.clear)
        clock = mock.patch("keel_booking.availability.timezone.now", return_value=NOW)
        clock.start()
        self.addCleanup(clock.stop)
        self.et = make_type(slug="paid", name="Paid call", price="$100", requires_approval=True, requires_payment=True)
        PaymentMethod.objects.create(name="USDT", amount_text="{amount} USDT", details="Network: ERC20\nAddress: 0xabc",
                                     note="Only ERC20.")
        PaymentMethod.objects.create(name="Lira", amount_text="The equivalent of {price} in lira",
                                     details="IBAN: TR00 1234")
        self.calls = []

        def fake_call(method, http_timeout=15, **data):
            self.calls.append((method, data))
            return {"message_id": 7} if method == "sendMessage" else True

        patcher = mock.patch("keel_booking.telegram.call", side_effect=fake_call)
        patcher.start()
        self.addCleanup(patcher.stop)
        body = {"start": "2026-11-02T07:00:00Z", "name": "Sara", "email": "sara@example.com",
                "timezone": "Europe/Istanbul", "telegram": "@sara_k"}
        self.assertEqual(self.client.post("/book/paid/reserve/", json.dumps(body),
                                          content_type="application/json").status_code, 201)
        self.booking = Booking.objects.get()

    def press(self, action):
        from keel_booking import telegram

        telegram.handle_update({"update_id": 1, "callback_query": {
            "id": "q", "data": f"kb:{action}:{self.booking.uid}", "message": {"message_id": 7, "chat": {"id": 42}}}})
        self.booking.refresh_from_db()

    def test_receipt_shows_the_steps(self, _now):
        body = outbox.outbox[0].body
        self.assertIn("[>] 2. I review it", body)
        self.assertIn("3. You pay", body)

    def test_approve_sends_payment_details_without_a_link(self, _now):
        outbox.outbox.clear()
        self.press("approve")
        self.assertEqual(self.booking.status, Booking.Status.PAYMENT)
        self.assertEqual(self.booking.payment_due_at, self.booking.start_at)  # the start is sooner than 24 hours
        self.assertEqual(len(outbox.outbox), 1)
        email = outbox.outbox[0]
        self.assertIn("100 USDT", email.body)
        self.assertIn("The equivalent of $100 in lira", email.body)
        self.assertIn("0xabc", email.alternatives[0][0])
        self.assertIn("[>] 3. You pay", email.body)
        self.assertIn("[x] 1. You sent the request", email.body)
        self.assertIn("[x] 2. I approved the request", email.body)
        self.assertNotIn("meet.google", email.body)
        keyboard = json.dumps([d for m, d in self.calls if m == "editMessageText"][-1]["reply_markup"])
        self.assertIn("kb:paid:", keyboard)
        self.assertEqual(self.client.post("/book/paid/reserve/", json.dumps({}), content_type="application/json").status_code, 422)

    def test_payment_received_confirms_with_the_link(self, _now):
        self.press("approve")
        self.booking.meet_url = "https://meet.google.com/abc-defg-hij"
        self.booking.save()
        outbox.outbox.clear()
        self.press("paid")
        self.assertEqual(self.booking.status, Booking.Status.CONFIRMED)
        self.assertIsNotNone(self.booking.paid_at)
        guest = [m for m in outbox.outbox if m.to == ["sara@example.com"]]
        self.assertIn("https://meet.google.com/abc-defg-hij", guest[0].body)
        self.assertEqual(len([m for m in outbox.outbox if m.to == ["owner@example.com"]]), 1)

    def test_unpaid_time_is_released(self, _now):
        self.press("approve")
        outbox.outbox.clear()
        services.tick(now=self.booking.payment_due_at + timedelta(minutes=1))
        self.booking.refresh_from_db()
        self.assertEqual(self.booking.status, Booking.Status.CANCELLED)
        self.assertIn("did not arrive in time", outbox.outbox[0].body)
