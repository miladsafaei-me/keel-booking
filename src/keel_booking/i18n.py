"""Language helpers shared by the pages and the emails: digits, direction, week start and dates.

The texts themselves are Django translations (``locale/<code>/LC_MESSAGES/django.po``); these
helpers cover what gettext cannot: the digits a language writes numbers with, its direction, the
first day of its week, and a date written out in words.
"""

from __future__ import annotations

from datetime import datetime

from django.utils import dateformat, translation
from django.utils.translation import gettext as _

# Languages that write numbers with their own digits, and those digits from 0 to 9.
DIGITS = {"fa": "۰۱۲۳۴۵۶۷۸۹", "ar": "٠١٢٣٤٥٦٧٨٩"}
# The first day of the week where it is not Monday, as JavaScript counts days (0 is Sunday).
WEEK_STARTS = {"fa": 6, "ar": 6, "he": 0, "en-us": 0}


def current(language: str | None = None) -> str:
    return (language or translation.get_language() or "en").lower()


def base(language: str | None = None) -> str:
    return current(language).split("-")[0]


def digits(value, language: str | None = None) -> str:
    """``value`` as text, its ASCII digits written in the language's own digits."""
    text = str(value)
    table = DIGITS.get(base(language))
    return text.translate(str.maketrans("0123456789", table)) if table else text


def is_rtl(language: str | None = None) -> bool:
    try:
        return translation.get_language_info(base(language))["bidi"]
    except KeyError:
        return False


def week_start(language: str | None = None) -> int:
    code = current(language)
    return WEEK_STARTS.get(code, WEEK_STARTS.get(code.split("-")[0], 1))


def long_date(moment: datetime) -> str:
    """Weekday, day, month and year in the active language, such as «Monday, 2 November 2026»."""
    return digits(_("{weekday}, {day} {month} {year}").format(
        weekday=dateformat.format(moment, "l"), day=moment.day, month=dateformat.format(moment, "F"), year=moment.year))


def short_date(moment: datetime) -> str:
    """A short date and time for subjects, such as «Mon 2 November, 10:00»."""
    return digits(_("{weekday} {day} {month}, {time}").format(
        weekday=dateformat.format(moment, "D"), day=moment.day, month=dateformat.format(moment, "F"),
        time=f"{moment:%H:%M}"))


def time_range(start: datetime, end: datetime) -> str:
    return digits(_("{start} to {end}").format(start=f"{start:%H:%M}", end=f"{end:%H:%M}"))
