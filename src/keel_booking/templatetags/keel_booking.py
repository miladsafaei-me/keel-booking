"""``{% load keel_booking %}``: numbers in the active language's digits, and its direction."""

from django import template

from .. import i18n

register = template.Library()


@register.filter
def local_digits(value):
    """``{{ event_type.duration_minutes|local_digits }}``: 45 on an English page, ۴۵ on a Persian one."""
    return i18n.digits(value)


@register.simple_tag
def text_direction():
    """``rtl`` or ``ltr`` for the active language."""
    return "rtl" if i18n.is_rtl() else "ltr"
