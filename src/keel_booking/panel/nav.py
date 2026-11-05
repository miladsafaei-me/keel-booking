from django.urls import reverse


def _item(label, icon, name, **extra):
    url = reverse(f"keel_booking_panel:{name}")
    return {"label": label, "icon": icon, "url": url, "match_prefix": url, **extra}


def booking_section(request):
    """The sidebar section of the booking pages, for the host's ``nav`` callable."""
    return {"label": "Bookings", "items": [
        _item("Bookings", "calendar", "bookings"),
        _item("Follow-ups", "bell", "follow_ups"),
        _item("Meeting types", "clock", "event_types"),
        _item("Payment methods", "credit-card", "payment_methods"),
        _item("Google", "plug", "google"),
    ]}
