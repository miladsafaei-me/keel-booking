"""Dashboard pieces for ``KEEL_WEB["admin_shell"]["dashboard_cards"]`` and ``["dashboard_panels"]``."""
from __future__ import annotations

from django.urls import reverse
from django.utils import timezone

from ..models import Booking, FollowUp


def cards(request):
    now = timezone.now()
    live = Booking.objects.exclude(status__in=(Booking.Status.CANCELLED, Booking.Status.DECLINED))
    base = reverse("keel_booking_panel:bookings")
    return [
        {"label": "Upcoming meetings", "icon": "calendar", "url": base,
         "value": live.filter(end_at__gt=now, status=Booking.Status.CONFIRMED).count()},
        {"label": "Requests waiting", "icon": "bell", "url": base + "?when=requests",
         "value": live.filter(status__in=(Booking.Status.PENDING, Booking.Status.PAYMENT)).count()},
        {"label": "Open follow-ups", "icon": "clock", "url": reverse("keel_booking_panel:follow_ups"),
         "value": FollowUp.objects.filter(is_done=False).count(),
         "note": f"{FollowUp.objects.filter(is_done=False, due_at__lte=now).count()} due"},
    ]


def panels(request):
    now = timezone.now()
    upcoming = (Booking.objects.select_related("event_type").filter(end_at__gt=now)
                .exclude(status__in=(Booking.Status.CANCELLED, Booking.Status.DECLINED)).order_by("start_at")[:6])
    return [{"title": "Next meetings", "url": reverse("keel_booking_panel:bookings"), "empty": "No meetings ahead.",
             "rows": [{"label": f"{b.guest_name} · {b.event_type.name}",
                       "meta": f"{timezone.localtime(b.start_at):%a %d %b, %H:%M}" + ("" if b.status == Booking.Status.CONFIRMED else f" · {b.get_status_display()}"),
                       "url": reverse("keel_booking_panel:booking", args=[b.pk])} for b in upcoming]}]
