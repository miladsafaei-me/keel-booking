"""Google: the OAuth connection, free/busy reads, calendar events with Meet links, and Gmail sending.

Plain REST over ``requests``, no Google client library. One Google account is connected from
the admin (``GoogleAccount``); its refresh token is stored and every call refreshes the access
token when it is about to expire.
"""

from __future__ import annotations

import base64
import logging
import secrets
import uuid
from datetime import datetime, timedelta, timezone as dt_timezone
from urllib.parse import urlencode

import requests
from django.core.cache import cache
from django.urls import reverse
from django.utils import timezone, translation
from django.utils.dateparse import parse_datetime
from django.utils.translation import gettext as _

from . import conf
from .models import GoogleAccount

log = logging.getLogger(__name__)

AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
USERINFO_URL = "https://openidconnect.googleapis.com/v1/userinfo"
CALENDAR_API = "https://www.googleapis.com/calendar/v3"
GMAIL_SEND_URL = "https://gmail.googleapis.com/gmail/v1/users/me/messages/send"
SCOPES = (
    "openid",
    "email",
    "https://www.googleapis.com/auth/calendar.events",
    "https://www.googleapis.com/auth/calendar.freebusy",
    "https://www.googleapis.com/auth/gmail.send",
)
MEET_SETTINGS_SCOPE = "https://www.googleapis.com/auth/meetings.space.settings"
MEET_API = "https://meet.googleapis.com/v2"
TIMEOUT = 20


def scopes() -> tuple[str, ...]:
    """The permissions to ask for: the base set, plus Meet settings when MEET_ACCESS is set."""
    return SCOPES + ((MEET_SETTINGS_SCOPE,) if conf.get("MEET_ACCESS") else ())


class GoogleError(Exception):
    pass


def is_configured() -> bool:
    return bool(conf.get("GOOGLE_CLIENT_ID") and conf.get("GOOGLE_CLIENT_SECRET"))


def authorization_url(redirect_uri: str) -> tuple[str, str]:
    """The consent URL and the state value to keep in the session."""
    state = secrets.token_urlsafe(24)
    params = {
        "client_id": conf.get("GOOGLE_CLIENT_ID"),
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": " ".join(scopes()),
        "access_type": "offline",
        "prompt": "consent",
        "include_granted_scopes": "true",
        "state": state,
    }
    return f"{AUTH_URL}?{urlencode(params)}", state


def exchange_code(code: str, redirect_uri: str) -> GoogleAccount:
    """Trade the callback's code for tokens and store them on the one GoogleAccount."""
    response = requests.post(TOKEN_URL, timeout=TIMEOUT, data={
        "code": code,
        "client_id": conf.get("GOOGLE_CLIENT_ID"),
        "client_secret": conf.get("GOOGLE_CLIENT_SECRET"),
        "redirect_uri": redirect_uri,
        "grant_type": "authorization_code",
    })
    if response.status_code != 200:
        raise GoogleError(f"Google refused the code: {response.text[:300]}")
    data = response.json()
    if not data.get("refresh_token"):
        raise GoogleError("Google sent no refresh token. Remove the app's access at "
                          "myaccount.google.com/permissions, then connect again.")
    account = GoogleAccount.load()
    account.refresh_token = data["refresh_token"]
    account.access_token = data["access_token"]
    account.token_expiry = timezone.now() + timedelta(seconds=int(data.get("expires_in", 3600)))
    account.scopes = data.get("scope", "")
    account.connected_at = timezone.now()
    account.last_error = ""
    info = requests.get(USERINFO_URL, timeout=TIMEOUT, headers={"Authorization": f"Bearer {account.access_token}"})
    if info.status_code == 200:
        account.email = info.json().get("email", "")
    account.save()
    return account


def disconnect() -> None:
    account = GoogleAccount.load()
    if account.refresh_token:
        try:
            requests.post("https://oauth2.googleapis.com/revoke", timeout=TIMEOUT, data={"token": account.refresh_token})
        except requests.RequestException:
            pass
    account.refresh_token = account.access_token = account.scopes = ""
    account.token_expiry = account.connected_at = None
    account.save()


def _access_token(account: GoogleAccount) -> str:
    if account.access_token and account.token_expiry and account.token_expiry > timezone.now() + timedelta(minutes=2):
        return account.access_token
    response = requests.post(TOKEN_URL, timeout=TIMEOUT, data={
        "client_id": conf.get("GOOGLE_CLIENT_ID"),
        "client_secret": conf.get("GOOGLE_CLIENT_SECRET"),
        "refresh_token": account.refresh_token,
        "grant_type": "refresh_token",
    })
    if response.status_code != 200:
        account.last_error = f"Token refresh failed: {response.text[:300]}"
        account.save(update_fields=["last_error"])
        raise GoogleError(account.last_error)
    data = response.json()
    account.access_token = data["access_token"]
    account.token_expiry = timezone.now() + timedelta(seconds=int(data.get("expires_in", 3600)))
    account.save(update_fields=["access_token", "token_expiry"])
    return account.access_token


def _call(method: str, url: str, account: GoogleAccount | None = None, **kwargs) -> requests.Response:
    account = account or GoogleAccount.load()
    if not account.is_connected:
        raise GoogleError("No Google account is connected.")
    headers = {"Authorization": f"Bearer {_access_token(account)}"}
    try:
        response = requests.request(method, url, headers=headers, timeout=TIMEOUT, **kwargs)
    except requests.RequestException as error:
        raise GoogleError(f"Google did not answer: {error}") from error
    if response.status_code >= 400:
        raise GoogleError(f"{method} {url.split('?')[0]} answered {response.status_code}: {response.text[:400]}")
    return response


def _rfc3339(moment: datetime) -> str:
    return moment.astimezone(dt_timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def busy_ranges(start: datetime, end: datetime, fresh: bool = False) -> list[tuple[datetime, datetime]]:
    """Busy time on every checked calendar. Empty when Google is not connected or does not answer.

    A failure is recorded on the account (the admin shows it) rather than raised, so the booking
    pages keep working on local bookings alone while Google is away.
    """
    account = GoogleAccount.load()
    if not account.is_connected:
        return []
    key = f"keel_booking:freebusy:{_rfc3339(start)}:{_rfc3339(end)}"
    if not fresh:
        cached = cache.get(key)
        if cached is not None:
            return cached
    try:
        response = _call("POST", f"{CALENDAR_API}/freeBusy", account, json={
            "timeMin": _rfc3339(start),
            "timeMax": _rfc3339(end),
            "items": [{"id": calendar_id} for calendar_id in account.calendars_to_check()],
        })
    except GoogleError as error:
        log.warning("free/busy failed: %s", error)
        GoogleAccount.objects.filter(pk=account.pk).update(last_error=str(error)[:1000])
        return []
    ranges = []
    for calendar in response.json().get("calendars", {}).values():
        for block in calendar.get("busy", []):
            ranges.append((parse_datetime(block["start"]), parse_datetime(block["end"])))
    cache.set(key, ranges, conf.get("FREEBUSY_CACHE_SECONDS"))
    return ranges


def event_body(booking) -> dict:
    """The calendar event, written in the language of the booking's owner side (mail.owner_language)."""
    from . import i18n, mail

    with translation.override(mail.owner_language(booking)):
        return _event_body(booking, booking.event_type.localized(), i18n)


def _event_body(booking, et, i18n) -> dict:
    attendees = [{"email": a.email, "displayName": a.name} if a.name else {"email": a.email}
                 for a in booking.attendees.all()]
    lines = [_("Booked by {name} <{email}>").format(name=booking.guest_name, email=booking.guest_email),
             _("Language: {language}").format(language=booking.language_name())]
    if booking.guest_company:
        lines.append(_("Brand: {brand}").format(brand=booking.guest_company))
    if booking.guest_phone:
        lines.append(_("Phone: {phone}").format(phone=booking.guest_phone))
    for name, handle in booking.socials_display():
        lines.append(f"{name}: {handle}")
    if booking.files.exists():
        lines.append(_("Files: {count} attached (in the booking)").format(count=i18n.digits(booking.files.count())))
    if booking.guest_message:
        lines += ["", booking.guest_message]
    lines += ["", _("Manage: {url}").format(url=conf.absolute(reverse("admin:keel_booking_booking_change", args=[booking.pk])))]
    body = {
        "summary": f"{et.name}: {booking.guest_name}",
        "description": "\n".join(lines),
        "start": {"dateTime": _rfc3339(booking.start_at), "timeZone": et.timezone},
        "end": {"dateTime": _rfc3339(booking.end_at), "timeZone": et.timezone},
        "attendees": attendees,
        "reminders": {"useDefault": True},
        "extendedProperties": {"private": {"keel_booking_uid": str(booking.uid)}},
    }
    if et.create_meet_link:
        body["conferenceData"] = {"createRequest": {
            "requestId": str(uuid.uuid5(uuid.NAMESPACE_URL, str(booking.uid))),
            "conferenceSolutionKey": {"type": "hangoutsMeet"},
        }}
    return body


def create_event(booking) -> dict:
    account = GoogleAccount.load()
    params = {"conferenceDataVersion": 1, "sendUpdates": conf.get("GOOGLE_SEND_UPDATES")}
    response = _call("POST", f"{CALENDAR_API}/calendars/{account.calendar_id}/events", account,
                     params=params, json=event_body(booking))
    return response.json()


def meet_url_of(event: dict) -> str:
    if event.get("hangoutLink"):
        return event["hangoutLink"]
    for point in event.get("conferenceData", {}).get("entryPoints", []):
        if point.get("entryPointType") == "video":
            return point.get("uri", "")
    return ""


def meet_code(meet_url: str) -> str:
    return meet_url.rstrip("/").rsplit("/", 1)[-1] if meet_url else ""


def can_set_meet_access(account: GoogleAccount | None = None) -> bool:
    account = account or GoogleAccount.load()
    return account.is_connected and MEET_SETTINGS_SCOPE in account.scopes.split()


def set_meet_access(meet_url: str, access: str) -> None:
    """Set who may enter one Meet without knocking (OPEN, TRUSTED or RESTRICTED)."""
    # Edits need the space's resource name (spaces/<id>); the meeting code only reads.
    name = _call("GET", f"{MEET_API}/spaces/{meet_code(meet_url)}").json()["name"]
    _call("PATCH", f"{MEET_API}/{name}", params={"updateMask": "config.accessType"},
          json={"config": {"accessType": access}})


def push_attendees(event_id: str, emails: list[tuple[str, str]]) -> None:
    """Make the event's guest list match ``emails`` ((email, name) pairs), keeping the account's own entry.

    Google then counts each of them as invited, which is what lets them into the Meet without knocking.
    """
    account = GoogleAccount.load()
    url = f"{CALENDAR_API}/calendars/{account.calendar_id}/events/{event_id}"
    current = _call("GET", url, account).json().get("attendees", [])
    kept = [a for a in current if a.get("self") or a.get("organizer")]
    by_email = {a.get("email", "").lower(): a for a in current}
    wanted = []
    for email, name in emails:
        entry = dict(by_email.get(email.lower(), {"email": email}))
        if name and not entry.get("displayName"):
            entry["displayName"] = name
        wanted.append(entry)
    seen = {a.get("email", "").lower() for a in kept}
    attendees = kept + [a for a in wanted if a["email"].lower() not in seen]
    _call("PATCH", url, account, params={"sendUpdates": conf.get("GOOGLE_SEND_UPDATES")}, json={"attendees": attendees})


def delete_event(event_id: str) -> None:
    account = GoogleAccount.load()
    try:
        _call("DELETE", f"{CALENDAR_API}/calendars/{account.calendar_id}/events/{event_id}", account,
              params={"sendUpdates": conf.get("GOOGLE_SEND_UPDATES")})
    except GoogleError as error:
        if "answered 410" not in str(error) and "answered 404" not in str(error):
            raise


def send_raw_email(raw_message: bytes) -> None:
    """Send one RFC 822 message through the connected account's Gmail."""
    _call("POST", GMAIL_SEND_URL, json={"raw": base64.urlsafe_b64encode(raw_message).decode()})
