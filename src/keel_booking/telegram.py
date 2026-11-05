"""The owner's Telegram bot: each meeting request arrives with Approve and Decline buttons.

The booking worker long-polls the bot (``poll``), so the presses need no public webhook URL.
Only presses from ``TELEGRAM_CHAT_ID`` count. A press decides the request through ``services``,
and the message is then rewritten with the outcome and without its buttons.
"""

from __future__ import annotations

import html
import logging

import requests
from django.conf import settings
from django.utils import translation
from django.utils.translation import gettext as _

from . import conf, i18n

log = logging.getLogger(__name__)

API = "https://api.telegram.org/bot{token}/{method}"
APPROVE, DECLINE, PAID = "approve", "decline", "paid"


class TelegramError(Exception):
    pass


def is_configured() -> bool:
    return bool(conf.get("TELEGRAM_BOT_TOKEN") and conf.get("TELEGRAM_CHAT_ID"))


def call(method: str, http_timeout: float = 15, **data):
    token = conf.get("TELEGRAM_BOT_TOKEN")
    if not token:
        raise TelegramError("No TELEGRAM_BOT_TOKEN.")
    try:
        response = requests.post(API.format(token=token, method=method), json=data, timeout=http_timeout)
        body = response.json()
    except (requests.RequestException, ValueError) as error:
        raise TelegramError(f"{method}: {error}") from error
    if not body.get("ok"):
        raise TelegramError(f"{method}: {body.get('description', response.status_code)}")
    return body.get("result")


def _language() -> str:
    return conf.get("TELEGRAM_LANGUAGE") or settings.LANGUAGE_CODE.split("-")[0]


def _e(value) -> str:
    return html.escape(str(value), quote=False)


def request_text(booking) -> str:
    """The request as the owner reads it: the session, the time, the guest, how to reach them, their answers."""
    from . import mail
    from .models import Booking

    event_type = booking.event_type.localized()
    comma = "، " if i18n.is_rtl() else ", "
    owner_when = mail.when(booking, booking.event_type.timezone)
    due = mail.payment_due_text(booking, booking.event_type.timezone) if booking.payment_due_at else ""
    status_line = {
        Booking.Status.PENDING: "🆕 " + _("New meeting request"),
        Booking.Status.PAYMENT: "💳 " + _("Approved. Waiting for the payment until {due}.").format(due=due),
        Booking.Status.CONFIRMED: "✅ " + _("Approved. The Meet link went to the guest by email."),
        Booking.Status.DECLINED: "❌ " + _("Declined. The guest was told by email."),
        Booking.Status.CANCELLED: "🚫 " + _("Cancelled"),
    }[booking.status]
    if booking.status == Booking.Status.CONFIRMED and booking.paid_at:
        status_line = "✅ " + _("Paid and confirmed. The Meet link went to the guest by email.")
    elif booking.status == Booking.Status.CANCELLED and booking.payment_due_at and not booking.paid_at:
        status_line = "⌛ " + _("The payment did not arrive in time. The time is free again.")
    duration = _("{minutes} min").format(minutes=i18n.digits(event_type.duration_minutes))
    price = f"{comma}{_e(event_type.price)}" if event_type.price else ""
    lines = [f"<b>{_e(status_line)}</b>", "", f"📌 <b>{_e(event_type.name)}</b> ({_e(duration)}{price})",
             f"🕒 {_e(owner_when['full'])}", f"     {_e(owner_when['timezone'])}"]
    if booking.guest_timezone != booking.event_type.timezone:
        guest_when = mail.when(booking, booking.guest_timezone)
        lines.append(f"     {_e(_('Guest time'))}: {_e(guest_when['time'])}{comma}{_e(guest_when['timezone'])}")
    lines += ["", f"👤 <b>{_e(booking.guest_name)}</b>" + (f"{comma}{_e(booking.guest_company)}" if booking.guest_company else ""),
              f"✉️ {_e(booking.guest_email)}"]
    if booking.guest_phone:
        lines.append(f"📞 {_e(booking.guest_phone)}")
    for network, value, link in booking.messenger_links():
        lines.append(f'💬 {network}: <a href="{html.escape(link)}">{_e(value)}</a>')
    for network, handle in booking.socials_display():
        lines.append(f"🔗 {_e(network)}: {_e(handle)}")
    answers = booking.answers_display(_language())
    if answers:
        lines.append("")
        lines += [f"• <b>{_e(question)}</b>: {_e(answer)}" for question, answer in answers]
    if booking.guest_message:
        lines += ["", f"📝 {_e(booking.guest_message[:1500])}"]
    files = booking.files.count()
    if files:
        lines.append(f"📎 {_e(_('Files'))}: {i18n.digits(files)}")
    lines += ["", f"🌐 {_e(_('Language'))}: {_e(booking.language_name())}"]
    return "\n".join(lines)[:4000]


def _keyboard(booking) -> dict:
    admin = conf.manage_url(booking)
    rows = []
    if booking.status == booking.Status.PENDING:
        rows.append([{"text": "✅ " + _("Approve"), "callback_data": f"kb:{APPROVE}:{booking.uid}"},
                     {"text": "❌ " + _("Decline"), "callback_data": f"kb:{DECLINE}:{booking.uid}"}])
    elif booking.status == booking.Status.PAYMENT:
        rows.append([{"text": "💰 " + _("Payment received"), "callback_data": f"kb:{PAID}:{booking.uid}"},
                     {"text": "❌ " + _("Cancel"), "callback_data": f"kb:{DECLINE}:{booking.uid}"}])
    if admin.startswith("https://"):
        rows.append([{"text": _("Open in the admin"), "url": admin}])
    return {"inline_keyboard": rows}


def send_request(booking) -> None:
    """Send the owner one message about a new request and remember it, so a decision can rewrite it."""
    with translation.override(_language()):
        result = call("sendMessage", chat_id=conf.get("TELEGRAM_CHAT_ID"), text=request_text(booking),
                      parse_mode="HTML", disable_web_page_preview=True, reply_markup=_keyboard(booking))
    booking.telegram_message_id = result["message_id"]
    type(booking).objects.filter(pk=booking.pk).update(telegram_message_id=booking.telegram_message_id)


def refresh_request(booking) -> None:
    """Rewrite the owner's message with the booking's present status; a decided request loses its buttons."""
    if not booking.telegram_message_id or not is_configured():
        return
    with translation.override(_language()):
        try:
            call("editMessageText", chat_id=conf.get("TELEGRAM_CHAT_ID"), message_id=booking.telegram_message_id,
                 text=request_text(booking), parse_mode="HTML", disable_web_page_preview=True,
                 reply_markup=_keyboard(booking))
        except TelegramError as error:
            if "not modified" not in str(error):
                log.warning("Telegram edit failed for %s: %s", booking.uid, error)


def handle_update(update: dict) -> None:
    """One update from the bot: a button press on a request, or a message (answered with the chat ID while unset)."""
    from . import services
    from .models import Booking

    owner_chat = str(conf.get("TELEGRAM_CHAT_ID"))
    message = update.get("message")
    if message:
        chat = str(message.get("chat", {}).get("id", ""))
        if not owner_chat:
            log.warning("Telegram chat %s wrote to the bot; set TELEGRAM_CHAT_ID to it to receive requests.", chat)
        return
    press = update.get("callback_query")
    if not press:
        return
    chat = str((press.get("message") or {}).get("chat", {}).get("id", ""))
    with translation.override(_language()):
        if not owner_chat or chat != owner_chat:
            call("answerCallbackQuery", callback_query_id=press["id"], text=_("This bot answers its owner only."))
            return
        parts = str(press.get("data", "")).split(":")
        booking = None
        if len(parts) == 3 and parts[0] == "kb" and parts[1] in (APPROVE, DECLINE, PAID):
            booking = Booking.objects.select_related("event_type").filter(uid=parts[2]).first()
        if booking is None:
            call("answerCallbackQuery", callback_query_id=press["id"], text=_("This request no longer exists."))
            return
        if parts[1] == APPROVE:
            done = services.approve_booking(booking)
            if booking.event_type.requires_payment:
                note = _("Approved. The guest gets the payment details by email.")
            else:
                note = _("Approved. The guest gets the Meet link by email.")
            note = note if done else _("This request was already decided.")
        elif parts[1] == PAID:
            done = services.mark_paid(booking)
            note = (_("Payment received. The guest gets the Meet link by email.") if done
                    else _("This request was already decided."))
        else:
            done = services.decline_booking(booking)
            note = _("Declined. The guest is told by email.") if done else _("This request was already decided.")
        call("answerCallbackQuery", callback_query_id=press["id"], text=note)
        booking.refresh_from_db()
        if booking.telegram_message_id is None:
            booking.telegram_message_id = (press.get("message") or {}).get("message_id")
        refresh_request(booking)


def poll(offset: int | None, timeout: int) -> int | None:
    """Wait up to ``timeout`` seconds for updates, handle them, and return the next offset."""
    params = {"timeout": timeout, "allowed_updates": ["message", "callback_query"]}
    if offset is not None:
        params["offset"] = offset
    for update in call("getUpdates", http_timeout=timeout + 10, **params) or []:
        offset = update["update_id"] + 1
        try:
            handle_update(update)
        except Exception:
            log.exception("Telegram update %s failed", update.get("update_id"))
    return offset
