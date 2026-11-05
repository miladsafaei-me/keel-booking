"""keel_booking.panel: the owner's pages inside the keel-web staff panel."""
from datetime import time, timedelta
from unittest import mock

from django.contrib.auth.models import User
from django.core import mail
from django.test import TestCase, override_settings
from django.utils import timezone

from keel_booking import conf
from keel_booking.models import AvailabilityWindow, Booking, EventQuestion, EventType, FollowUp, PaymentMethod

SHELL = {"admin_shell": {"brand_name": "Owner", "home_url": "keel_booking_panel:bookings", "logout_url": None,
                         "profile_url": None, "client_url": None, "nav": "tests.panel_urls.nav",
                         "dashboard_cards": ("keel_booking.panel.dashboard.cards",)}}


@override_settings(ROOT_URLCONF="tests.panel_urls", LOGIN_URL="login", KEEL_WEB=SHELL)
class PanelTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.owner = User.objects.create_superuser("owner", "owner@example.com", "pw")
        cls.kind = EventType.objects.create(slug="intro", name="Intro call", duration_minutes=30)
        AvailabilityWindow.objects.create(event_type=cls.kind, weekday=0, start_time=time(9), end_time=time(17))
        start = timezone.now() + timedelta(days=2)
        cls.booking = Booking.objects.create(event_type=cls.kind, start_at=start, end_at=start + timedelta(minutes=30),
                                             guest_name="Ada", guest_email="ada@example.com",
                                             status=Booking.Status.PENDING)

    def setUp(self):
        self.client.force_login(self.owner)

    def test_staff_only(self):
        self.client.logout()
        response = self.client.get("/panel/bookings/")
        self.assertEqual(response.status_code, 302)
        self.assertIn("/panel/login/", response["Location"])

    def test_every_page_renders_with_the_sidebar(self):
        question = EventQuestion.objects.create(event_type=self.kind, key="stage", label="Stage")
        method = PaymentMethod.objects.create(name="Bank", details="IBAN: TR00")
        FollowUp.objects.create(booking=self.booking, due_at=timezone.now(), note="Call")
        for url in ("/panel/bookings/", "/panel/bookings/?when=requests", "/panel/bookings/?when=all&q=ada",
                    f"/panel/bookings/{self.booking.pk}/", "/panel/bookings/follow-ups/",
                    "/panel/bookings/follow-ups/?show=all", "/panel/bookings/types/", "/panel/bookings/types/new/",
                    f"/panel/bookings/types/{self.kind.pk}/", f"/panel/bookings/questions/{question.pk}/",
                    "/panel/bookings/payments/", "/panel/bookings/payments/new/",
                    f"/panel/bookings/payments/{method.pk}/", "/panel/bookings/google/"):
            response = self.client.get(url)
            self.assertEqual(response.status_code, 200, url)
            self.assertContains(response, 'href="/panel/bookings/types/"', msg_prefix=url)

    def test_requests_tab_lists_the_request(self):
        self.assertContains(self.client.get("/panel/bookings/?when=requests"), "Ada")

    def test_note_and_follow_up(self):
        url = f"/panel/bookings/{self.booking.pk}/"
        self.client.post(url, {"part": "note", "note-body": "Prefers mornings"})
        self.assertEqual(self.booking.notes.get().body, "Prefers mornings")
        self.client.post(url, {"part": "follow", "follow-due_at": "2030-01-01T10:00", "follow-note": "Send deck"})
        row = self.booking.follow_ups.get()
        self.client.post(url, {"part": "follow_done", "id": row.pk})
        row.refresh_from_db()
        self.assertTrue(row.is_done)

    def test_decline_action(self):
        with mock.patch("keel_booking.telegram.refresh_request"):
            self.client.post(f"/panel/bookings/{self.booking.pk}/", {"part": "decline"})
        self.booking.refresh_from_db()
        self.assertEqual(self.booking.status, Booking.Status.DECLINED)

    def test_event_type_saves_with_its_rows(self):
        url = f"/panel/bookings/types/{self.kind.pk}/"
        page = self.client.get(url)
        data = {}
        form = page.context["form"]
        for name, field in form.fields.items():
            value = form[name].value()
            if value is None or value is False or value == "":
                continue
            data[name] = "on" if value is True else value
        data["name"] = "Intro call, renamed"
        for s in page.context["sets"]:
            fs = s["formset"]
            data.update({f"{fs.prefix}-{k}": v for k, v in fs.management_form.initial.items()})
            for i, sub in enumerate(fs.forms):
                for name in sub.fields:
                    value = sub[name].value()
                    if not (value is None or value is False or value == ""):
                        data[f"{fs.prefix}-{i}-{name}"] = "on" if value is True else (value.strftime("%H:%M") if hasattr(value, "strftime") else value)
        data["windows-1-weekday"] = "2"
        data["windows-1-start_time"] = "10:00"
        data["windows-1-end_time"] = "12:00"
        response = self.client.post(url, data)
        self.assertEqual(response.status_code, 302, getattr(response, "context", {}) and [
            (s["title"], s["formset"].errors) for s in response.context["sets"]])
        self.kind.refresh_from_db()
        self.assertEqual(self.kind.name, "Intro call, renamed")
        self.assertEqual(self.kind.windows.count(), 2)

    def test_owner_links_point_to_the_panel(self):
        self.assertEqual(conf.manage_url(self.booking), f"https://example.com/panel/bookings/{self.booking.pk}/")

    @override_settings(KEEL_BOOKING={**conf.DEFAULTS, "SITE_URL": "https://example.com",
                                     "GOOGLE_CLIENT_ID": "id", "GOOGLE_CLIENT_SECRET": "s",
                                     "PANEL_GOOGLE_CALLBACK": "legacy_google_callback"})
    def test_google_redirect_uri_follows_the_alias(self):
        self.assertContains(self.client.get("/panel/bookings/google/"), "https://example.com/old/oauth/callback/")
        response = self.client.get("/panel/bookings/google/connect/")
        self.assertIn("redirect_uri=https%3A%2F%2Fexample.com%2Fold%2Foauth%2Fcallback%2F", response["Location"])

    def test_delete_type_refuses_with_bookings_by_default(self):
        self.client.post(f"/panel/bookings/types/{self.kind.pk}/", {"part": "delete"})
        self.assertTrue(EventType.objects.filter(pk=self.kind.pk).exists())

    def test_delete_type_with_bookings_when_the_host_says_so(self):
        with self.settings(KEEL_BOOKING={**conf.DEFAULTS, "SITE_URL": "https://example.com",
                                         "PANEL_DELETE_TYPE_WITH_BOOKINGS": True}):
            self.client.post(f"/panel/bookings/types/{self.kind.pk}/", {"part": "delete"})
        self.assertFalse(EventType.objects.filter(pk=self.kind.pk).exists())
        self.assertFalse(Booking.objects.exists())
