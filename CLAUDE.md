# keel-booking: package guide

Part of the **Keel** platform (see keel-kit `PLATFORM.md`). A reusable Django app for booking
meetings: meeting types, the slot engine, Google Calendar/Meet/Gmail, emails, reminders, the
owner's notes and follow-ups. Guest-facing text is translated (English source, Persian and Turkish
shipped in `locale/`); the admin and the owner's emails stay English. No banner comments; CSS
variables only in styling; multi-line Django template comments use `{% comment %}...{% endcomment %}`.

## Task tracking

Remaining and follow-up work is tracked in [TODO.md](TODO.md), not in chat memory.

## Boundaries

- **Here (generic):** models, `availability.py` (slot engine), `google.py` (OAuth + REST),
  `mail.py` (rendering, `.ics`, `GmailBackend`), `services.py` (create, sync, emails, cancel,
  `tick`), views + JSON endpoints, the admin, `booking_worker`, neutral templates, `booking.js`.
- **Stays in the host:** the look (its own templates and CSS on top of the `data-kb-*` contract),
  the meeting types themselves (seeded by a host data migration or the admin), `KEEL_BOOKING`
  values, the email theme, where the app is mounted and how it is deployed.

## Rules

- Tables are `keel_booking_*`, app label `keel_booking`. Never rename a released label or table.
- `booking.js` must keep binding to attributes only; a class name in the script breaks every host.
- Times are stored in UTC; weekly hours are wall-clock times in the type's timezone.
- Releases: `~/www/keel-kit/scripts/keel-release.sh X.Y.Z`; consumers re-pin the tag.
- Tests: `PYTHONPATH=src:. DJANGO_SETTINGS_MODULE=tests.settings django-admin test tests`.
- A new or changed guest-facing string is marked for translation (`{% translate %}`, `gettext`) and
  gets its Persian and Turkish in the same change: from `src/keel_booking`, run
  `PYTHONPATH=../..:.. DJANGO_SETTINGS_MODULE=tests.settings django-admin makemessages -l fa -l tr --no-location`,
  fill the `.po` files, then `django-admin compilemessages`. The `.mo` files are committed.
- Numbers in a guest-facing text go through `i18n.digits` (or the `local_digits` filter), so Persian
  reads them in Persian digits.

## Consumers

- `miladsafaei-site` (miladsafaei.me/book/).
