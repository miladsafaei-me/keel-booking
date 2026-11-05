"""The owner's pages: bookings with their notes, follow-ups, files and actions; meeting types with their
hours, questions and translations; payment methods; and the Google connection."""
from __future__ import annotations

from django.contrib import messages
from django.core.paginator import Paginator
from django.db import transaction
from django.db.models import Count, Q
from django.http import FileResponse, Http404, HttpResponseRedirect
from django.shortcuts import get_object_or_404, redirect
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_POST

from keel_web.admin_shell.context import render_panel, staff_required

from .. import conf, google, services
from ..models import Booking, BookingFile, EventQuestion, EventType, FollowUp, GoogleAccount, PaymentMethod
from . import forms as f

PRE = "Bookings"
STATUS_TONE = {Booking.Status.PENDING: "warning", Booking.Status.PAYMENT: "warning",
               Booking.Status.CONFIRMED: "success", Booking.Status.DECLINED: "error",
               Booking.Status.CANCELLED: "gray"}
WHEN = (("upcoming", "Upcoming"), ("requests", "Requests"), ("past", "Past"), ("all", "All"))


def _telegram_refresh(booking):
    from .. import telegram

    telegram.refresh_request(booking)


@staff_required
def booking_list(request):
    now = timezone.now()
    rows = (Booking.objects.select_related("event_type").annotate(files_total=Count("files", distinct=True)))
    when = request.GET.get("when", "upcoming")
    if when == "upcoming":
        rows = rows.filter(end_at__gt=now).exclude(status__in=(Booking.Status.CANCELLED, Booking.Status.DECLINED)).order_by("start_at")
    elif when == "requests":
        rows = rows.filter(status__in=(Booking.Status.PENDING, Booking.Status.PAYMENT)).order_by("start_at")
    elif when == "past":
        rows = rows.filter(end_at__lte=now).order_by("-start_at")
    else:
        when = "all"
        rows = rows.order_by("-start_at")
    q = request.GET.get("q", "").strip()
    if q:
        rows = rows.filter(Q(guest_name__icontains=q) | Q(guest_email__icontains=q) | Q(guest_company__icontains=q)
                           | Q(guest_phone__icontains=q) | Q(guest_message__icontains=q))
    status = request.GET.get("status", "")
    if status in Booking.Status.values:
        rows = rows.filter(status=status)
    kind = request.GET.get("type", "")
    if kind.isdigit():
        rows = rows.filter(event_type_id=int(kind))
    page = Paginator(rows, 50).get_page(request.GET.get("page"))
    for booking in page:
        booking.tone = STATUS_TONE.get(booking.status, "gray")
    query = "&".join(f"{k}={v}" for k, v in (("when", when), ("q", q), ("status", status), ("type", kind)) if v)
    return render_panel(request, "keel_booking_panel/booking_list.html", {
        "page_title": "Bookings", "page_pretitle": PRE, "page_obj": page, "when": when, "whens": WHEN, "q": q,
        "status": status, "statuses": Booking.Status.choices, "type": kind, "types": EventType.objects.all(),
        "query": query})


ACTIONS = {
    "approve": ("Approved: the calendar event, the Meet link and the confirmation email follow.",
                lambda b: b.status == Booking.Status.PENDING and services.approve_booking(b), True),
    "paid": ("Payment received: the calendar event, the Meet link and the confirmation email follow.",
             lambda b: b.status == Booking.Status.PAYMENT and services.mark_paid(b), True),
    "decline": ("Declined. The guest is told by email.",
                lambda b: b.status in (Booking.Status.PENDING, Booking.Status.PAYMENT) and services.decline_booking(b), True),
    "resync": ("Written to Google Calendar again.",
               lambda b: b.status == Booking.Status.CONFIRMED and services.sync_to_google(b), False),
    "push_guests": ("The calendar event's guests now match this booking.", None, False),
    "resend": ("The confirmation email is sent again.", None, False),
    "cancel": ("Cancelled, removed from the calendar, and the guests are told.", None, False),
}


def _run_action(request, booking, action):
    text, run, telegram = ACTIONS[action]
    if action == "push_guests":
        services.ensure_guest_attendee(booking)
        ok = booking.status == Booking.Status.CONFIRMED and services.push_attendees(booking)
        if ok:
            services.apply_meet_access(booking)
    elif action == "resend":
        ok = booking.status == Booking.Status.CONFIRMED
        if ok:
            booking.confirmation_sent_at = None
            booking.mail_attempts = 0
            booking.save(update_fields=["confirmation_sent_at", "mail_attempts"])
            services.send_new_booking_emails(booking)
    elif action == "cancel":
        services.cancel_booking(booking)
        ok = True
    else:
        ok = bool(run(booking))
    if ok and telegram:
        _telegram_refresh(booking)
    if ok:
        messages.success(request, text)
    else:
        messages.warning(request, "That did not happen. The booking's state does not allow it, or Google answered "
                                  "with an error (see Delivery).")


@staff_required
def booking_detail(request, pk):
    booking = get_object_or_404(Booking.objects.select_related("event_type"), pk=pk)
    guest_form = f.BookingGuestForm(instance=booking, prefix="guest")
    attendees = f.AttendeeFormSet(instance=booking, prefix="attendees")
    note_form = f.NoteForm(prefix="note")
    follow_form = f.FollowUpForm(prefix="follow")
    if request.method == "POST":
        part = request.POST.get("part", "")
        back = HttpResponseRedirect(reverse("keel_booking_panel:booking", args=[booking.pk]))
        if part in ACTIONS:
            _run_action(request, booking, part)
            return back
        if part == "guest":
            guest_form = f.BookingGuestForm(request.POST, instance=booking, prefix="guest")
            attendees = f.AttendeeFormSet(request.POST, instance=booking, prefix="attendees")
            if guest_form.is_valid() and attendees.is_valid():
                with transaction.atomic():
                    guest_form.save()
                    for row in attendees.save(commit=False):
                        if not row.pk:
                            row.role = row.Role.ADDITIONAL
                        row.save()
                    for row in attendees.deleted_objects:
                        row.delete()
                services.ensure_guest_attendee(booking)
                if booking.google_event_id and booking.status == Booking.Status.CONFIRMED:
                    if services.push_attendees(booking):
                        messages.success(request, "Saved. The calendar event's guests now match, so each can enter the Meet.")
                    else:
                        messages.warning(request, "Saved, but the calendar event could not be updated; see Delivery.")
                else:
                    messages.success(request, "Saved.")
                return back
        elif part == "note":
            note_form = f.NoteForm(request.POST, prefix="note")
            if note_form.is_valid():
                booking.notes.create(body=note_form.cleaned_data["body"])
                messages.success(request, "Note added.")
                return back
        elif part == "follow":
            follow_form = f.FollowUpForm(request.POST, prefix="follow")
            if follow_form.is_valid():
                row = follow_form.save(commit=False)
                row.booking = booking
                row.save()
                messages.success(request, "Follow-up added. Its reminder email goes out when it is due.")
                return back
        elif part == "follow_done":
            row = get_object_or_404(FollowUp, pk=request.POST.get("id"), booking=booking)
            row.is_done = not row.is_done
            row.save(update_fields=["is_done"])
            return back
        elif part == "note_delete":
            booking.notes.filter(pk=request.POST.get("id")).delete()
            return back
        elif part == "file_delete":
            item = get_object_or_404(BookingFile, pk=request.POST.get("id"), booking=booking)
            item.file.delete(save=False)
            item.delete()
            messages.success(request, "File deleted.")
            return back
    actions = []
    if booking.status == Booking.Status.PENDING:
        actions += [("approve", "Approve", "primary"), ("decline", "Decline", "danger")]
    if booking.status == Booking.Status.PAYMENT:
        actions += [("paid", "Payment received", "primary"), ("decline", "Decline", "danger")]
    if booking.status == Booking.Status.CONFIRMED:
        actions += [("resync", "Write to Google Calendar again", ""), ("push_guests", "Copy the guests to the event", ""),
                    ("resend", "Send the confirmation again", ""), ("cancel", "Cancel the meeting", "danger")]
    return render_panel(request, "keel_booking_panel/booking_detail.html", {
        "page_title": booking.guest_name, "page_pretitle": f"{booking.event_type.name} · "
        f"{timezone.localtime(booking.start_at):%Y-%m-%d %H:%M}", "booking": booking,
        "tone": STATUS_TONE.get(booking.status, "gray"), "guest_form": guest_form, "attendees": attendees,
        "note_form": note_form, "follow_form": follow_form, "actions": actions,
        "follow_ups": booking.follow_ups.order_by("is_done", "due_at"), "notes": booking.notes.order_by("-created_at"),
        "files": booking.files.all()})


@staff_required
def booking_file(request, pk):
    """A guest's attachment, to staff only: the files are never on a public URL."""
    item = get_object_or_404(BookingFile, pk=pk)
    try:
        handle = item.file.open("rb")
    except FileNotFoundError:
        raise Http404
    return FileResponse(handle, as_attachment=True, filename=item.original_name)


@staff_required
def follow_up_list(request):
    show = request.GET.get("show", "open")
    rows = FollowUp.objects.select_related("booking", "booking__event_type")
    rows = rows.filter(is_done=False).order_by("due_at") if show == "open" else rows.order_by("-due_at")
    if request.method == "POST":
        row = get_object_or_404(FollowUp, pk=request.POST.get("id"))
        row.is_done = not row.is_done
        row.save(update_fields=["is_done"])
        return redirect(f"{reverse('keel_booking_panel:follow_ups')}?show={show}")
    return render_panel(request, "keel_booking_panel/follow_up_list.html", {
        "page_title": "Follow-ups", "page_pretitle": PRE, "page_obj": Paginator(rows, 50).get_page(request.GET.get("page")),
        "show": show, "query": f"show={show}", "now": timezone.now()})


@staff_required
def event_type_list(request):
    if request.method == "POST":
        row = get_object_or_404(EventType, pk=request.POST.get("id"))
        row.is_active = not row.is_active
        row.save(update_fields=["is_active"])
        messages.success(request, f"{row.name} is {'on' if row.is_active else 'off'}.")
        return redirect("keel_booking_panel:event_types")
    rows = EventType.objects.annotate(booking_total=Count("bookings")).order_by("sort_order", "name")
    for row in rows:
        try:
            row.page_url = reverse("keel_booking:event_type", args=[row.slug])
        except Exception:
            row.page_url = ""
    return render_panel(request, "keel_booking_panel/event_type_list.html", {
        "page_title": "Meeting types", "page_pretitle": PRE, "rows": rows})


def _type_formsets(data, instance):
    sets = {
        "windows": (f.WindowFormSet, "Weekly hours", "The days and hours this meeting can start, in its timezone."),
        "overrides": (f.OverrideFormSet, "Date exceptions", "A closed day, or other hours on one date."),
        "fixed": (f.FixedAttendeeFormSet, "Fixed attendees", "People invited to every meeting of this type."),
        "questions": (f.TypeQuestionFormSet, "Form questions", "Asked on the booking form. Translate each on its own page."),
        "translations": (f.TypeTranslationFormSet, "Translations", "The page and email texts in each other language; an empty text keeps the English."),
    }
    built = []
    for prefix, (cls, title, note) in sets.items():
        formset = cls(data, instance=instance, prefix=prefix)
        if prefix == "questions":
            for form in formset:
                if form.instance.pk:
                    form.row_link = reverse("keel_booking_panel:question", args=[form.instance.pk])
                    form.row_link_label = "Translations"
        built.append({"formset": formset, "title": title, "note": note, "anchor": f"type-{prefix}"})
    return built


@staff_required
def event_type_edit(request, pk=None):
    instance = get_object_or_404(EventType, pk=pk) if pk else EventType()
    data = request.POST if request.method == "POST" else None
    if data is not None and data.get("part") == "delete" and pk:
        total = instance.bookings.count()
        if total and not conf.get("PANEL_DELETE_TYPE_WITH_BOOKINGS"):
            messages.error(request, f"{instance.name} has {total} bookings, so it cannot be deleted. Switch it off instead.")
            return redirect("keel_booking_panel:event_type", instance.pk)
        with transaction.atomic():
            instance.bookings.all().delete()
            instance.delete()
        messages.success(request, f"{instance.name} is deleted" + (f", with its {total} bookings." if total else "."))
        return redirect("keel_booking_panel:event_types")
    form = f.EventTypeForm(data, instance=instance)
    sets = _type_formsets(data, instance) if pk else []
    if data is not None and form.is_valid() and all(s["formset"].is_valid() for s in sets):
        with transaction.atomic():
            row = form.save()
            for s in sets:
                s["formset"].save()
        messages.success(request, f"{row.name} is saved.")
        return redirect("keel_booking_panel:event_type", row.pk)
    return render_panel(request, "keel_booking_panel/event_type_form.html", {
        "page_title": instance.name if pk else "New meeting type", "page_pretitle": PRE, "form": form,
        "sets": sets, "row": instance if pk else None, "booking_total": instance.bookings.count() if pk else 0,
        "delete_with_bookings": conf.get("PANEL_DELETE_TYPE_WITH_BOOKINGS")})


@staff_required
def question_edit(request, pk):
    question = get_object_or_404(EventQuestion.objects.select_related("event_type"), pk=pk)
    data = request.POST if request.method == "POST" else None
    form = f.QuestionForm(data, instance=question)
    translations = f.QuestionTranslationFormSet(data, instance=question, prefix="translations")
    if data is not None:
        if data.get("part") == "delete":
            type_pk = question.event_type_id
            question.delete()
            messages.success(request, "Question deleted.")
            return redirect("keel_booking_panel:event_type", type_pk)
        if form.is_valid() and translations.is_valid():
            with transaction.atomic():
                form.save()
                translations.save()
            messages.success(request, "Question saved.")
            return redirect("keel_booking_panel:question", question.pk)
    return render_panel(request, "keel_booking_panel/question_form.html", {
        "page_title": question.label, "page_pretitle": question.event_type.name, "form": form,
        "translations": translations, "question": question})


@staff_required
def payment_method_list(request):
    return render_panel(request, "keel_booking_panel/payment_method_list.html", {
        "page_title": "Payment methods", "page_pretitle": PRE,
        "rows": PaymentMethod.objects.order_by("sort_order", "name")})


@staff_required
def payment_method_edit(request, pk=None):
    instance = get_object_or_404(PaymentMethod, pk=pk) if pk else PaymentMethod()
    data = request.POST if request.method == "POST" else None
    if data is not None and data.get("part") == "delete" and pk:
        instance.delete()
        messages.success(request, "Payment method deleted.")
        return redirect("keel_booking_panel:payment_methods")
    form = f.PaymentMethodForm(data, instance=instance)
    translations = f.PaymentTranslationFormSet(data, instance=instance, prefix="translations") if pk else None
    if data is not None and form.is_valid() and (translations is None or translations.is_valid()):
        with transaction.atomic():
            row = form.save()
            if translations is not None:
                translations.save()
        messages.success(request, f"{row.name} is saved.")
        return redirect("keel_booking_panel:payment_method", row.pk)
    return render_panel(request, "keel_booking_panel/payment_method_form.html", {
        "page_title": instance.name if pk else "New payment method", "page_pretitle": PRE, "form": form,
        "translations": translations, "row": instance if pk else None})


def _redirect_uri(request) -> str:
    path = reverse(conf.get("PANEL_GOOGLE_CALLBACK"))
    return conf.absolute(path) if conf.get("SITE_URL") else request.build_absolute_uri(path)


@staff_required
def google_page(request):
    account = GoogleAccount.load()
    form = f.GoogleCalendarsForm(request.POST or None, instance=account)
    if request.method == "POST" and form.is_valid():
        form.save()
        messages.success(request, "Calendars saved.")
        return redirect("keel_booking_panel:google")
    return render_panel(request, "keel_booking_panel/google.html", {
        "page_title": "Google connection", "page_pretitle": PRE, "account": account, "form": form,
        "google_configured": google.is_configured(), "redirect_uri": _redirect_uri(request), "scopes": google.scopes()})


@staff_required
def google_connect(request):
    if not google.is_configured():
        messages.error(request, "Set GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET in KEEL_BOOKING first.")
        return redirect("keel_booking_panel:google")
    url, state = google.authorization_url(_redirect_uri(request))
    request.session["keel_booking_oauth_state"] = state
    return HttpResponseRedirect(url)


@staff_required
def google_callback(request):
    expected = request.session.pop("keel_booking_oauth_state", None)
    if not expected or request.GET.get("state") != expected:
        messages.error(request, "The Google answer did not match this session. Please connect again.")
    elif request.GET.get("error"):
        messages.error(request, f"Google said: {request.GET['error']}")
    else:
        try:
            account = google.exchange_code(request.GET.get("code", ""), _redirect_uri(request))
            messages.success(request, f"Connected {account.email}. New meetings now go on its calendar with a Meet link.")
        except google.GoogleError as error:
            messages.error(request, str(error))
    return redirect("keel_booking_panel:google")


@staff_required
@require_POST
def google_disconnect(request):
    google.disconnect()
    messages.success(request, "Google is disconnected.")
    return redirect("keel_booking_panel:google")
