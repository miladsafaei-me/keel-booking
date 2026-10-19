"""The host's KEEL_BOOKING settings, with the package defaults filled in.

A host sets only what differs, for example::

    KEEL_BOOKING = {
        "OWNER_NAME": "Jane Doe",
        "OWNER_EMAIL": "jane@example.com",
        "OWNER_TIMEZONE": "Europe/Istanbul",
        "SITE_URL": "https://example.com",
        "GOOGLE_CLIENT_ID": os.environ.get("GOOGLE_OAUTH_CLIENT_ID", ""),
        "GOOGLE_CLIENT_SECRET": os.environ.get("GOOGLE_OAUTH_CLIENT_SECRET", ""),
    }
"""

from __future__ import annotations

from django.conf import settings

DEFAULTS = {
    # Who the meetings are with. OWNER_EMAIL gets every owner notification and follow-up reminder.
    "OWNER_NAME": "",
    "OWNER_EMAIL": "",
    # The default availability timezone of a new meeting type.
    "OWNER_TIMEZONE": "UTC",
    # Absolute origin used for links inside emails, without a trailing slash.
    "SITE_URL": "",
    # The From header of every booking email. Empty means "OWNER_NAME <OWNER_EMAIL>".
    "FROM_EMAIL": "",
    # Google OAuth web client. The redirect URI to register is shown on the admin's Google page.
    "GOOGLE_CLIENT_ID": "",
    "GOOGLE_CLIENT_SECRET": "",
    # Google's own invitation emails: "none" because the package sends its own branded ones;
    # "all" makes Google email every attendee as well.
    "GOOGLE_SEND_UPDATES": "none",
    # Seconds a free/busy answer from Google is reused for the slot pages.
    "FREEBUSY_CACHE_SECONDS": 60,
    # How many extra guest emails a visitor may add to one booking.
    "MAX_ADDITIONAL_GUESTS": 5,
    # Fields the visitor must fill. Any of: name, email, company (shown as "brand"), phone, socials, message.
    "REQUIRED_FIELDS": ("name", "email"),
    # The networks a visitor can pick for a social profile, as (value, label).
    "SOCIAL_NETWORKS": (("linkedin", "LinkedIn"), ("instagram", "Instagram"), ("telegram", "Telegram"),
                        ("x", "X"), ("youtube", "YouTube"), ("github", "GitHub"), ("website", "Website"),
                        ("other", "Other")),
    "MAX_SOCIALS": 6,
    # Files a visitor can attach: how many, how large each, and which extensions.
    "MAX_FILES": 5,
    "MAX_FILE_MB": 10,
    "FILE_EXTENSIONS": ("pdf", "doc", "docx", "ppt", "pptx", "key", "xls", "xlsx", "csv", "txt", "md", "rtf",
                        "png", "jpg", "jpeg", "webp", "gif", "zip"),
    # Attach the guest's files to the owner's new-booking email while their total stays under this.
    "OWNER_ATTACH_MB": 15,
    # Reservations one IP address may make per hour.
    "RESERVE_RATE_PER_HOUR": 10,
    # Paths under the booking root that can never be a meeting type's slug.
    "RESERVED_SLUGS": ("admin", "static", "confirmed", "api", "media"),
    # The template every booking page extends. A host points it at its own layout.
    "BASE_TEMPLATE": "keel_booking/base.html",
    # Inline colours and fonts of the HTML emails (email clients ignore CSS variables).
    "EMAIL_THEME": {
        "ground": "#F3F4F6",
        "card": "#FFFFFF",
        "rule": "#E5E7EB",
        "heading": "#111827",
        "text": "#374151",
        "muted": "#6B7280",
        "primary": "#4F46E5",
        "on_primary": "#FFFFFF",
        "font_heading": "Arial, Helvetica, sans-serif",
        "font_body": "Arial, Helvetica, sans-serif",
        "logo_url": "",
        "brand_name": "",
    },
}


def get(key: str):
    """One KEEL_BOOKING value, falling back to the package default."""
    value = getattr(settings, "KEEL_BOOKING", {}).get(key, DEFAULTS[key])
    if key == "EMAIL_THEME":
        return {**DEFAULTS["EMAIL_THEME"], **value}
    return value


def from_email() -> str:
    explicit = get("FROM_EMAIL")
    if explicit:
        return explicit
    name, email = get("OWNER_NAME"), get("OWNER_EMAIL")
    return f"{name} <{email}>" if name else email


def absolute(path: str) -> str:
    """An absolute URL for a site path, for links that leave the browser (emails, Google)."""
    return get("SITE_URL").rstrip("/") + path
