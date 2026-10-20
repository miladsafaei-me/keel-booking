# keel-booking

Meeting booking for a Keel site, in the spirit of Calendly: meeting types with their own pages
and rules, a slot engine, Google Calendar and Meet sync, branded emails, reminders, and the
owner's notes and follow-ups.

## What it does

- **Meeting types** (`EventType`): slug, page copy, duration, buffers before and after, start-time
  step, minimum notice, how far ahead, daily limit, availability timezone, visitor timezone
  detection, reminder lead time, Meet on or off, extra guests on or off.
- **Hours:** weekly windows per weekday (`AvailabilityWindow`, several per day) and date overrides
  (`DateOverride`: a closed day, or other hours on one date).
- **Fixed attendees** (`FixedAttendee`): people invited to every meeting of a type.
- **Form questions** (`EventQuestion`): a type's own questions on top of the standard fields: short or
  long text, a dropdown, radio options or checkboxes, each optional or required, with translations
  (`EventQuestionTranslation`). Answers are stored on the booking by question key (option keys for
  choices) and shown in the admin, the owner's email and the calendar event.
- **Slots:** weekly hours in the type's timezone, minus busy time on the connected Google
  calendars, minus every confirmed booking (all types, each with its buffers), within notice and
  window, under the daily limit. The reservation re-checks inside a Postgres advisory lock with
  Google read fresh, so one slot is never sold twice.
- **Google:** one account connected from the admin with OAuth (calendar events, free/busy, Gmail
  send). Each booking becomes a calendar event with a Google Meet link and every attendee.
- **Emails:** confirmation to the guest, the guests they added and fixed attendees (with an `.ics`),
  a notice to the owner, a reminder to everyone N minutes before, a follow-up reminder to the owner,
  and a cancellation. `keel_booking.mail.GmailBackend` sends them through the connected Gmail.
- **Owner side:** Django admin. Notes and follow-ups are inlines on a booking; actions re-write to
  Google, resend the confirmation, or cancel.
- **Background work:** `manage.py booking_worker` runs `services.tick()` every minute (calendar
  retries, unsent emails, reminders, follow-ups). `booking_tick` runs one pass.

## Install

```
keel-booking @ git+https://github.com/miladsafaei-me/keel-booking@v0.1.0
```

```python
INSTALLED_APPS += ["keel_booking"]
EMAIL_BACKEND = "keel_booking.mail.GmailBackend"   # or any SMTP backend
TIME_ZONE = "Europe/Istanbul"                       # the admin shows times in this zone
KEEL_BOOKING = {
    "OWNER_NAME": "Jane Doe",
    "OWNER_EMAIL": "jane@example.com",
    "OWNER_TIMEZONE": "Europe/Istanbul",
    "SITE_URL": "https://example.com",
    "GOOGLE_CLIENT_ID": os.environ.get("GOOGLE_OAUTH_CLIENT_ID", ""),
    "GOOGLE_CLIENT_SECRET": os.environ.get("GOOGLE_OAUTH_CLIENT_SECRET", ""),
    "BASE_TEMPLATE": "site/booking_base.html",
    "EMAIL_THEME": {"primary": "#4F4FE6", "logo_url": "https://example.com/avatar.png"},
}
```

```python
urlpatterns = [
    path("book/admin/", admin.site.urls),
    path("book/", include("keel_booking.urls")),
]
```

Every key and its default is in `src/keel_booking/conf.py`.

## Languages

Every guest-facing word is a Django translation; the package ships Persian (`fa`) and Turkish (`tr`)
beside the English source. Mount the pages inside `i18n_patterns` with `LocaleMiddleware`, and
each language gets its own pages (`/book/`, `/fa/book/`, `/tr/book/`):

```python
LANGUAGES = [("en", "English"), ("tr", "Türkçe"), ("fa", "فارسی")]
urlpatterns = [path("book/admin/", admin.site.urls)] + i18n_patterns(
    path("book/", include("keel_booking.urls")), prefix_default_language=False)
```

- **Meeting types** take a translation per language in the admin (`EventTypeTranslation`: name,
  tagline, description, highlights, confirmation note). An empty text keeps the type's own.
- **A booking keeps its language** (`Booking.language`, the page it was made on). Every email of
  the booking is written in it: the guest's confirmation, reminder and cancellation, the `.ics` and
  Google Calendar link, and, with `OWNER_EMAILS_IN_BOOKING_LANGUAGE` on, the owner's new-booking
  notice, reminder and follow-up and the calendar event's text too (off: those use `LANGUAGE_CODE`).
  Persian emails are right to left in the theme's `font_rtl`. The admin stays in `LANGUAGE_CODE`.
- **Dates and numbers:** weekday and month names come from Django's own translations; Persian
  writes numbers in Persian digits (`i18n.digits`, the `local_digits` filter). The widget formats
  with the page's `lang` on the Gregorian calendar, starts the week on `config.weekStart`
  (Saturday for Persian) and takes its sentences from `config.labels`.
- `OWNER_NAME` and `EMAIL_THEME["brand_name"]` may be lazy translations (`gettext_lazy("Jane Doe")`) to spell
  the owner's name per language; leave `FROM_EMAIL` empty and the From name follows it.

## Pages and endpoints

| Path (under the mount) | What |
|---|---|
| `` | the list of active meeting types |
| `<slug>/` | one type's page: calendar, slots, form |
| `<slug>/slots/?month=YYYY-MM&tz=Area/City` | JSON, open UTC start times grouped by the visitor's date (noindex) |
| `<slug>/reserve/` | POST JSON, creates the booking (CSRF, honeypot, per-IP rate limit) |
| `confirmed/<uuid>/` | the confirmation page (noindex) |

Slugs in `RESERVED_SLUGS` (`admin`, `static`, `confirmed`, `api`, `media`) are refused.

## Theming

Override `keel_booking/landing.html`, `event_type.html` and `confirmed.html` in the host. The
widget script `keel_booking/booking.js` binds only to `data-kb-*` attributes and clones the host's
`<template data-kb-template="weekday|blank|day|slot">` elements, so the host owns every element and
class; keep the attributes from the package's `event_type.html`. The questions render from
`keel_booking/_questions.html`; override it to style them, keeping each input's `name="q_<key>"` and
its `data-kb-field-error="q_<key>"`. Emails take their colours, fonts
and logo from `EMAIL_THEME`, or override `keel_booking/email/*.html|txt`.

## Google setup (once per site)

1. Google Cloud console: enable the Google Calendar API and the Gmail API.
2. OAuth consent screen: External, publish it ("In production") so the refresh token does not
   expire after seven days. The scopes are listed on the admin's Google page.
3. Credentials: an OAuth client of type "Web application" with the redirect URI the admin's Google
   page shows (`<SITE_URL><admin mount>keel_booking/googleaccount/oauth/callback/`).
4. Put the client ID and secret in the environment, open the admin's Google page, press Connect.

## Tests

```
PYTHONPATH=src:. DJANGO_SETTINGS_MODULE=tests.settings django-admin test tests
```
