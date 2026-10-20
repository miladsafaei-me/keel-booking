"""Public booking pages: the list of meeting types, one type's page, its slots, the reservation, the confirmation.

Templates are host-overridable (``keel_booking/landing.html``, ``event_type.html``,
``confirmed.html``); each extends ``KEEL_BOOKING["BASE_TEMPLATE"]``.

Every page speaks the active language: mount the URLs inside ``i18n_patterns`` and each language
gets its own pages, its meeting-type translations, and a booking whose emails follow it.
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
from django.utils.translation import gettext as _
from django.views.decorators.cache import never_cache
from django.views.decorators.csrf import ensure_csrf_cookie
from django.views.decorators.http import require_GET, require_POST

from . import availability, conf, i18n, mail, services
from .models import Booking, EventType

NOINDEX = "noindex, nofollow"


def _context(**extra) -> dict:
    return {"base_template": conf.get("BASE_TEMPLATE"), "owner_name": conf.get("OWNER_NAME"), **extra}


def _active_types():
    return EventType.objects.filter(is_active=True).prefetch_related("translations")


def _questions(event_type) -> list:
    """The type's own form questions, in the active language."""
    return [q.localized() for q in event_type.questions.prefetch_related("translations")]


def _language() -> str:
    """The active language when the site offers it, for the booking's emails; else empty."""
    from django.conf import settings

    code = i18n.base()
    return code if code in {c.split("-")[0] for c, _name in settings.LANGUAGES} else ""


def _labels() -> dict:
    """The widget's own words (booking.js), in the active language."""
    return {
        "dayOpen": _("{day}, times available"),
        "dayClosed": _("{day}, no times"),
        "pickDay": _("Pick a day"),
        "loadFailed": _("The calendar did not load. Please refresh the page."),
        "fixFields": _("Please check the highlighted fields."),
        "failed": _("The booking did not go through. Please try again."),
        "tooManyFiles": _("You can attach up to {n} files."),
        "badFileType": _("{name}: this file type is not accepted."),
        "fileTooLarge": _("{name} is larger than {mb} MB."),
        "summary": _("{day}, {start} to {end}"),
    }


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
    types = [t.localized() for t in _active_types()]
    return render(request, "keel_booking/landing.html", _context(event_types=types))


@ensure_csrf_cookie
def event_type_page(request, slug):
    event_type = get_object_or_404(_active_types(), slug=slug).localized()
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
        "weekStart": i18n.week_start(),
        "labels": _labels(),
    }
    return render(request, "keel_booking/event_type.html", _context(
        event_type=event_type, config=config, required=set(conf.get("REQUIRED_FIELDS")),
        questions=_questions(event_type),
        social_networks=conf.get("SOCIAL_NETWORKS"),
        accept=",".join(f".{ext}" for ext in conf.get("FILE_EXTENSIONS")),
        other_types=[t.localized() for t in _active_types().exclude(pk=event_type.pk)]))


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
        errors["socials"] = _("You can add up to {n} profiles.").format(n=i18n.digits(conf.get("MAX_SOCIALS")))
    return socials


def _clean_files(files, errors) -> list:
    limit, max_mb = conf.get("MAX_FILES"), conf.get("MAX_FILE_MB")
    extensions = {ext.lower() for ext in conf.get("FILE_EXTENSIONS")}
    if len(files) > limit:
        errors["files"] = _("You can attach up to {n} files.").format(n=i18n.digits(limit))
        return []
    for upload in files:
        ext = upload.name.rsplit(".", 1)[-1].lower() if "." in upload.name else ""
        if ext not in extensions:
            errors["files"] = _("{name}: this file type is not accepted.").format(name=upload.name)
        elif upload.size > max_mb * 1024 * 1024:
            errors["files"] = _("{name} is larger than {mb} MB.").format(name=upload.name, mb=i18n.digits(max_mb))
    return files


def _clean_answers(event_type, data, post, errors) -> dict:
    """The answers to the type's own questions, by key; a choice keeps only offered option keys."""
    answers = {}
    for question in event_type.questions.all():
        name = question.field_name
        keys = [key for key, _label in question.option_list()]
        if question.kind == question.Kind.CHECKBOX:
            raw = post.getlist(name) if post is not None else data.get(name) or []
            raw = [raw] if isinstance(raw, str) else raw
            picked = {str(v) for v in raw}
            value = [key for key in keys if key in picked]
        else:
            value = str(data.get(name, "")).strip()[:4000 if question.kind == question.Kind.TEXTAREA else 300]
            if question.is_choice and value and value not in keys:
                errors[name] = _("Please pick one of the options.")
                continue
        if value:
            answers[question.key] = value
        elif question.required:
            errors[name] = _("Please pick an option.") if question.is_choice else _("Please fill this in.")
    return answers


@require_POST
def reserve(request, slug):
    event_type = get_object_or_404(EventType, slug=slug, is_active=True)
    try:
        data, uploads = _form_data(request)
    except ValueError:
        return _json({"error": "invalid", "message": _("The form could not be read.")}, 400)
    if data.get("website"):
        return _json({"error": "invalid", "message": _("The form could not be read.")}, 400)

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
            errors[field_name] = _("Please fill this in.")
    if phone and not all(ch.isdigit() or ch in "+-() ." for ch in phone):
        errors["phone"] = _("Please enter a phone number with digits only.")
    try:
        validate_email(email)
    except ValidationError:
        errors["email"] = _("Please enter a valid email address.")
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
            errors["guests"] = _("{email} is not a valid email address.").format(email=guest)
    if len(additional) > limit:
        errors["guests"] = _("You can add up to {n} guests.").format(n=i18n.digits(limit))
    files = _clean_files(uploads, errors)
    answers = _clean_answers(event_type, data, None if request.content_type == "application/json" else request.POST,
                             errors)
    start = parse_datetime(str(data.get("start", "")))
    if not start or start.tzinfo is None:
        errors["start"] = _("Please pick a time.")
    if errors:
        return _json({"error": "invalid", "fields": errors}, 422)
    if _rate_limited(request):
        return _json({"error": "rate", "message": _("Too many bookings from this connection. Please try again later.")}, 429)

    guest = services.GuestDetails(
        name=name, email=email, timezone=_zone(data.get("timezone"), event_type.timezone), company=company,
        phone=phone, socials=socials, message=message, additional=additional, files=files, language=_language(),
        answers=answers)
    try:
        booking = services.create_booking(event_type, start, guest)
    except services.SlotTaken:
        return _json({"error": "taken", "message": _("That time was just taken. Please pick another one.")}, 409)
    return _json({"redirect": reverse("keel_booking:confirmed", args=[booking.uid])}, 201)


@never_cache
def confirmed(request, uid):
    booking = get_object_or_404(Booking.objects.select_related("event_type"), uid=uid)
    if booking.status != Booking.Status.CONFIRMED:
        raise Http404
    response = render(request, "keel_booking/confirmed.html", _context(
        booking=booking, event_type=booking.event_type.localized(), when=mail.when(booking, booking.guest_timezone),
        calendar_link=mail.google_calendar_link(booking), guests=booking.attendees.exclude(role="guest")))
    response["X-Robots-Tag"] = NOINDEX
    return response
