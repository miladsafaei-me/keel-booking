"""Public booking pages: the list of meeting types, one type's page, its slots, the reservation, the confirmation.

Templates are host-overridable (``keel_booking/landing.html``, ``event_type.html``,
``confirmed.html``); each extends ``KEEL_BOOKING["BASE_TEMPLATE"]``.
"""

from __future__ import annotations

import json
import zoneinfo
from datetime import date, datetime, time, timedelta

from django.core.cache import cache
from django.core.exceptions import ValidationError
from django.core.validators import validate_email
from django.http import Http404, JsonResponse
from django.middleware.csrf import get_token
from django.shortcuts import get_object_or_404, render
from django.urls import reverse
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from django.views.decorators.cache import never_cache
from django.views.decorators.csrf import ensure_csrf_cookie
from django.views.decorators.http import require_GET, require_POST

from . import availability, conf, mail, services
from .models import Booking, EventType

NOINDEX = "noindex, nofollow"


def _context(**extra) -> dict:
    return {"base_template": conf.get("BASE_TEMPLATE"), "owner_name": conf.get("OWNER_NAME"), **extra}


def _json(data: dict, status: int = 200) -> JsonResponse:
    response = JsonResponse(data, status=status)
    response["X-Robots-Tag"] = NOINDEX
    response["Cache-Control"] = "private, no-store"
    return response


def _zone(name: str | None, fallback: str) -> str:
    try:
        zoneinfo.ZoneInfo(name or "")
        return name
    except (zoneinfo.ZoneInfoNotFoundError, ValueError):
        return fallback


def landing(request):
    types = EventType.objects.filter(is_active=True)
    return render(request, "keel_booking/landing.html", _context(event_types=types))


@ensure_csrf_cookie
def event_type_page(request, slug):
    event_type = get_object_or_404(EventType, slug=slug, is_active=True)
    config = {
        "csrfToken": get_token(request),
        "slotsUrl": reverse("keel_booking:slots", args=[slug]),
        "reserveUrl": reverse("keel_booking:reserve", args=[slug]),
        "timezone": event_type.timezone,
        "detectTimezone": event_type.detect_visitor_timezone,
        "duration": event_type.duration_minutes,
        "maxGuests": conf.get("MAX_ADDITIONAL_GUESTS") if event_type.allow_additional_guests else 0,
        "maxSocials": conf.get("MAX_SOCIALS"),
        "maxFiles": conf.get("MAX_FILES"),
        "maxFileMb": conf.get("MAX_FILE_MB"),
        "fileExtensions": list(conf.get("FILE_EXTENSIONS")),
    }
    return render(request, "keel_booking/event_type.html", _context(
        event_type=event_type, config=config, required=set(conf.get("REQUIRED_FIELDS")),
        social_networks=conf.get("SOCIAL_NETWORKS"),
        accept=",".join(f".{ext}" for ext in conf.get("FILE_EXTENSIONS")),
        other_types=EventType.objects.filter(is_active=True).exclude(pk=event_type.pk)))


@require_GET
def slots(request, slug):
    """Open start times for one month, grouped by the visitor's local date.

    ``?month=YYYY-MM&tz=Area/City``. Times are UTC ISO strings; the page formats them.
    """
    event_type = get_object_or_404(EventType, slug=slug, is_active=True)
    tz_name = _zone(request.GET.get("tz"), event_type.timezone)
    tz = zoneinfo.ZoneInfo(tz_name)
    try:
        year, month = (int(part) for part in request.GET.get("month", "").split("-"))
        first = date(year, month, 1)
    except ValueError:
        first = timezone.now().astimezone(tz).date().replace(day=1)
    following = (first.replace(day=28) + timedelta(days=4)).replace(day=1)
    range_start = datetime.combine(first, time(0), tzinfo=tz)
    range_end = datetime.combine(following, time(0), tzinfo=tz)

    days: dict[str, list[str]] = {}
    for slot in availability.available_slots(event_type, range_start, range_end):
        key = slot.start.astimezone(tz).date().isoformat()
        days.setdefault(key, []).append(slot.start.astimezone(zoneinfo.ZoneInfo("UTC")).strftime("%Y-%m-%dT%H:%M:%SZ"))
    now_local = timezone.now().astimezone(tz)
    last = (timezone.now() + timedelta(days=event_type.max_days_ahead)).astimezone(tz).date()
    return _json({
        "month": first.strftime("%Y-%m"),
        "timezone": tz_name,
        "today": now_local.date().isoformat(),
        "firstMonth": now_local.date().replace(day=1).strftime("%Y-%m"),
        "lastMonth": last.replace(day=1).strftime("%Y-%m"),
        "days": days,
    })


def _rate_limited(request) -> bool:
    ip = request.META.get("HTTP_CF_CONNECTING_IP") or request.META.get("REMOTE_ADDR", "")
    key = f"keel_booking:reserve:{ip}"
    count = cache.get(key, 0)
    if count >= conf.get("RESERVE_RATE_PER_HOUR"):
        return True
    cache.set(key, count + 1, 3600)
    return False


def _form_data(request) -> tuple[dict, list]:
    """The reservation as a dict and its uploads: multipart from the page, or JSON from a script."""
    if request.content_type == "application/json":
        data = json.loads(request.body or b"{}")
        return data, []
    data = {key: request.POST.get(key, "") for key in request.POST}
    data["guests"] = request.POST.getlist("guests") or data.get("guests", "")
    networks = request.POST.getlist("social_network")
    handles = request.POST.getlist("social_handle")
    data["socials"] = [{"network": n, "handle": h} for n, h in zip(networks, handles)]
    return data, request.FILES.getlist("files")


def _clean_socials(raw, errors) -> list[dict]:
    allowed = {value for value, _ in conf.get("SOCIAL_NETWORKS")}
    socials = []
    for item in raw if isinstance(raw, list) else []:
        if not isinstance(item, dict):
            continue
        handle = str(item.get("handle", "")).strip()[:200]
        network = str(item.get("network", "")).strip()
        if handle:
            socials.append({"network": network if network in allowed else "other", "handle": handle})
    if len(socials) > conf.get("MAX_SOCIALS"):
        errors["socials"] = f"You can add up to {conf.get('MAX_SOCIALS')} profiles."
    return socials


def _clean_files(files, errors) -> list:
    limit, max_mb = conf.get("MAX_FILES"), conf.get("MAX_FILE_MB")
    extensions = {ext.lower() for ext in conf.get("FILE_EXTENSIONS")}
    if len(files) > limit:
        errors["files"] = f"You can attach up to {limit} files."
        return []
    for upload in files:
        ext = upload.name.rsplit(".", 1)[-1].lower() if "." in upload.name else ""
        if ext not in extensions:
            errors["files"] = f"{upload.name}: this file type is not accepted."
        elif upload.size > max_mb * 1024 * 1024:
            errors["files"] = f"{upload.name} is larger than {max_mb} MB."
    return files


@require_POST
def reserve(request, slug):
    event_type = get_object_or_404(EventType, slug=slug, is_active=True)
    try:
        data, uploads = _form_data(request)
    except ValueError:
        return _json({"error": "invalid", "message": "The form could not be read."}, 400)
    if data.get("website"):
        return _json({"error": "invalid", "message": "The form could not be read."}, 400)

    errors = {}
    required = set(conf.get("REQUIRED_FIELDS"))
    name = str(data.get("name", "")).strip()[:120]
    email = str(data.get("email", "")).strip()[:254]
    company = str(data.get("company", "")).strip()[:160]
    phone = str(data.get("phone", "")).strip()[:40]
    message = str(data.get("message", "")).strip()[:4000]
    socials = _clean_socials(data.get("socials"), errors)
    missing = {"name": name, "company": company, "phone": phone, "message": message, "socials": socials}
    for field_name, value in missing.items():
        if field_name in required and not value:
            errors[field_name] = "Please fill this in."
    if phone and not all(ch.isdigit() or ch in "+-() ." for ch in phone):
        errors["phone"] = "Please enter a phone number with digits only."
    try:
        validate_email(email)
    except ValidationError:
        errors["email"] = "Please enter a valid email address."
    additional = []
    limit = conf.get("MAX_ADDITIONAL_GUESTS") if event_type.allow_additional_guests else 0
    raw_guests = data.get("guests") or []
    if isinstance(raw_guests, str):
        raw_guests = [raw_guests]
    raw_guests = [part for g in raw_guests for part in str(g).replace(",", " ").split()]
    for guest in raw_guests:
        try:
            validate_email(guest)
            additional.append(guest)
        except ValidationError:
            errors["guests"] = f"{guest} is not a valid email address."
    if len(additional) > limit:
        errors["guests"] = f"You can add up to {limit} guests."
    files = _clean_files(uploads, errors)
    start = parse_datetime(str(data.get("start", "")))
    if not start or start.tzinfo is None:
        errors["start"] = "Please pick a time."
    if errors:
        return _json({"error": "invalid", "fields": errors}, 422)
    if _rate_limited(request):
        return _json({"error": "rate", "message": "Too many bookings from this connection. Please try again later."}, 429)

    guest = services.GuestDetails(
        name=name, email=email, timezone=_zone(data.get("timezone"), event_type.timezone), company=company,
        phone=phone, socials=socials, message=message, additional=additional, files=files)
    try:
        booking = services.create_booking(event_type, start, guest)
    except services.SlotTaken:
        return _json({"error": "taken", "message": "That time was just taken. Please pick another one."}, 409)
    return _json({"redirect": reverse("keel_booking:confirmed", args=[booking.uid])}, 201)


@never_cache
def confirmed(request, uid):
    booking = get_object_or_404(Booking.objects.select_related("event_type"), uid=uid)
    if booking.status != Booking.Status.CONFIRMED:
        raise Http404
    response = render(request, "keel_booking/confirmed.html", _context(
        booking=booking, event_type=booking.event_type, when=mail.when(booking, booking.guest_timezone),
        calendar_link=mail.google_calendar_link(booking), guests=booking.attendees.exclude(role="guest")))
    response["X-Robots-Tag"] = NOINDEX
    return response
