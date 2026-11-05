"""The booking assistant with Gemini replaced by a script: it finds times, books one, and the booking takes the
form's road (a held request, the owner told, the guest's receipt), with the payment methods for a paid type."""

import json
from datetime import datetime, timezone as dt_timezone
from unittest import mock

from django.core import mail as outbox
from django.core.cache import cache
from django.test import TestCase, override_settings
from django.conf import settings

from keel_booking import gemini
from keel_booking.models import Booking, PaymentMethod

from .test_booking import NOW, make_type

UTC = dt_timezone.utc
KEY = {**settings.KEEL_BOOKING, "ASSISTANT_GEMINI_KEY": "test", "ASSISTANT_EMAIL_OWNER": True}


def call(tool, **args):
    return {"role": "model", "parts": [{"functionCall": {"name": tool, "args": args}, "thoughtSignature": "sig"}]}


def text(words):
    return {"role": "model", "parts": [{"text": words}]}


@override_settings(KEEL_BOOKING=KEY)
class AssistantTests(TestCase):
    def setUp(self):
        cache.clear()
        self.type = make_type(price="$100", requires_approval=True, requires_payment=True)
        PaymentMethod.objects.create(name="Bank", amount_text="{price} in lira", details="IBAN: TR00 1234")
        self.seen = []

    def post(self, script, messages=None):
        def fake(system, contents, tools=None, temperature=0.6):
            self.seen.append(contents)
            return script.pop(0)
        body = {"messages": messages or [{"role": "assistant", "text": "Hi"}, {"role": "user", "text": "Book SEO"}],
                "timezone": "Europe/Istanbul"}
        with mock.patch("keel_booking.gemini.generate", side_effect=fake), \
                mock.patch("django.utils.timezone.now", return_value=NOW):
            return self.client.post("/booking/chat", json.dumps(body), content_type="application/json")

    def test_finds_times_then_books_a_paid_request(self):
        times = {}

        def find_then_book(system, contents, tools=None, temperature=0.6):
            self.seen.append(contents)
            if len(self.seen) == 1:
                return call("find_times", slug="seo", from_date="2026-11-02")
            if len(self.seen) == 2:
                times.update(contents[-1]["parts"][0]["functionResponse"]["response"])
                return call("book_session", slug="seo", date=times["times"][0]["date"], time=times["times"][0]["time"], name="Jane Doe",
                            email="jane@example.com", phone="+90 555 123 4567", company="Acme",
                            telegram="@janedoe", message="Grow organic traffic", visitor_confirmed=True)
            self.answer = contents[-1]["parts"][0]["functionResponse"]["response"]
            return text("Booked. Pay by bank.")

        body = {"messages": [{"role": "assistant", "text": "Hi"}, {"role": "user", "text": "Yes, book it"}],
                "timezone": "Europe/Istanbul"}
        with mock.patch("keel_booking.gemini.generate", side_effect=find_then_book), \
                mock.patch("django.utils.timezone.now", return_value=NOW):
            response = self.client.post("/booking/chat", json.dumps(body), content_type="application/json")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"reply": "Booked. Pay by bank.", "booked": True})
        self.assertTrue(times["times"])
        booking = Booking.objects.get()
        self.assertEqual(booking.status, Booking.Status.PENDING)
        self.assertEqual(booking.guest_telegram, "@janedoe")
        self.assertTrue(self.answer["paid"])
        self.assertEqual(self.answer["payment_methods"][0]["details"], ["IBAN: TR00 1234"])
        # The model's function call went back whole, signature included.
        self.assertEqual(self.seen[1][-2]["parts"][0]["thoughtSignature"], "sig")
        recipients = sorted(address for message in outbox.outbox for address in message.to)
        self.assertEqual(recipients, ["jane@example.com", "owner@example.com"])

    def test_refuses_to_book_without_the_visitors_yes(self):
        script = [call("book_session", slug="seo", date="2026-11-02", time="10:00", name="J", email="j@example.com",
                       phone="+905551234567", company="A", telegram="@janedoe", message="x", visitor_confirmed=False),
                  text("Please confirm.")]
        self.assertEqual(self.post(script).status_code, 200)
        self.assertFalse(Booking.objects.exists())
        self.assertEqual(self.seen[1][-1]["parts"][0]["functionResponse"]["response"]["error"], "not_confirmed")

    def test_no_model_answers_503(self):
        with mock.patch("keel_booking.gemini.generate", side_effect=gemini.Unavailable("x")):
            response = self.client.post("/booking/chat", json.dumps(
                {"messages": [{"role": "user", "text": "hi"}]}), content_type="application/json")
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json(), {"error": "unavailable"})

    def test_malformed_conversation_422(self):
        response = self.client.post("/booking/chat", json.dumps(
            {"messages": [{"role": "user", "text": "a"}, {"role": "user", "text": "b"}]}), content_type="application/json")
        self.assertEqual(response.status_code, 422)

    def test_persian_catalog_and_prompt(self):
        from keel_booking import assistant
        prompt = assistant.system_prompt("fa", "Asia/Tehran")
        self.assertIn("slug: seo", prompt)
        self.assertIn("Asia/Tehran", prompt)
        self.assertNotIn(chr(0x2014), prompt)

    def test_with_telegram_the_owner_also_gets_an_email(self):
        from keel_booking import assistant
        ctx = assistant.Context("en", "Europe/Istanbul", "1.2.3.4")
        with mock.patch("keel_booking.telegram.is_configured", return_value=True), \
                mock.patch("keel_booking.telegram.send_request") as sent, \
                mock.patch("django.utils.timezone.now", return_value=NOW):
            first = assistant.find_times(ctx, "seo")["times"][0]
            answer = assistant.book_session(ctx, slug="seo", date=first["date"], time=first["time"], name="Jane Doe", email="jane@example.com",
                                            phone="+905551234567", company="Acme", whatsapp="+905551234567",
                                            message="Traffic", visitor_confirmed=True)
        self.assertEqual(answer["status"], "requested")
        sent.assert_called_once()
        self.assertEqual(sorted(a for m in outbox.outbox for a in m.to), ["jane@example.com", "owner@example.com"])

    def test_a_taken_time_returns_nearby_times(self):
        from keel_booking import assistant
        ctx = assistant.Context("en", "Europe/Istanbul", "1.2.3.4")
        with mock.patch("django.utils.timezone.now", return_value=NOW):
            answer = assistant.book_session(ctx, slug="seo", date="2026-11-02", time="15:00", name="Jane Doe",
                                            email="jane@example.com", phone="+905551234567", company="Acme",
                                            telegram="@janedoe", message="Traffic", visitor_confirmed=True)
        self.assertEqual(answer["error"], "taken")
        self.assertEqual(answer["nearby"][0]["time"], "10:00")
        self.assertFalse(Booking.objects.exists())
