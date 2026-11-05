from __future__ import annotations

from django import forms
from django.forms import inlineformset_factory

from keel_web.admin_shell.forms import PanelFormMixin

from ..models import (AvailabilityWindow, Booking, BookingAttendee, DateOverride, EventQuestion,
                      EventQuestionTranslation, EventType, EventTypeTranslation, FixedAttendee, FollowUp,
                      GoogleAccount, PaymentMethod, PaymentMethodTranslation)


class PanelModelForm(PanelFormMixin, forms.ModelForm):
    pass


def _textareas(rows=2, *names):
    return {name: forms.Textarea(attrs={"rows": rows}) for name in names}


class BookingGuestForm(PanelModelForm):
    class Meta:
        model = Booking
        fields = ("guest_name", "guest_company", "guest_email", "guest_phone", "guest_telegram", "guest_whatsapp",
                  "guest_message")
        widgets = _textareas(4, "guest_message")


class NoteForm(PanelFormMixin, forms.Form):
    body = forms.CharField(label="New note", widget=forms.Textarea(attrs={"rows": 3}))


class FollowUpForm(PanelModelForm):
    class Meta:
        model = FollowUp
        fields = ("due_at", "note")
        widgets = {"due_at": forms.DateTimeInput(attrs={"type": "datetime-local"}, format="%Y-%m-%dT%H:%M"),
                   **_textareas(2, "note")}
        labels = {"due_at": "Due", "note": "What to do"}


class EventTypeForm(PanelModelForm):
    class Meta:
        model = EventType
        fields = ("name", "slug", "tagline", "price", "description", "highlights", "icon", "sort_order", "is_active",
                  "duration_minutes", "slot_step_minutes", "daily_limit", "buffer_before_minutes",
                  "buffer_after_minutes", "min_notice_minutes", "max_days_ahead", "timezone",
                  "detect_visitor_timezone", "create_meet_link", "allow_additional_guests", "reminder_minutes",
                  "requires_approval", "requires_payment", "confirmation_note")
        widgets = _textareas(3, "description", "highlights", "confirmation_note")

    # The page's sections, in order: (title, note, field names).
    sections = (
        ("Page", "What visitors read in the list of meetings and at the top of this meeting's page.",
         ("name", "slug", "tagline", "price", "description", "highlights", "icon", "sort_order", "is_active")),
        ("Length and booking window", "How long the meeting runs and which start times are offered.",
         ("duration_minutes", "slot_step_minutes", "daily_limit", "buffer_before_minutes", "buffer_after_minutes",
          "min_notice_minutes", "max_days_ahead", "timezone", "detect_visitor_timezone")),
        ("Meeting and emails", "",
         ("create_meet_link", "allow_additional_guests", "reminder_minutes", "requires_approval", "requires_payment",
          "confirmation_note")),
    )

    def section_fields(self):
        return [(title, note, [self[name] for name in names]) for title, note, names in self.sections]


class QuestionForm(PanelModelForm):
    class Meta:
        model = EventQuestion
        fields = ("event_type", "sort_order", "key", "kind", "required", "label", "help_text", "options")
        widgets = _textareas(4, "options")


class PaymentMethodForm(PanelModelForm):
    class Meta:
        model = PaymentMethod
        fields = ("name", "is_active", "sort_order", "amount_text", "details", "note")
        widgets = _textareas(4, "details")


class GoogleCalendarsForm(PanelModelForm):
    class Meta:
        model = GoogleAccount
        fields = ("calendar_id", "busy_calendar_ids")
        widgets = _textareas(3, "busy_calendar_ids")


def _formset(parent, model, fields, widgets=None, extra=1, form=PanelModelForm):
    return inlineformset_factory(parent, model, form=form, fields=fields, widgets=widgets or {}, extra=extra,
                                 can_delete=True)


TIME = {"start_time": forms.TimeInput(attrs={"type": "time"}, format="%H:%M"),
        "end_time": forms.TimeInput(attrs={"type": "time"}, format="%H:%M")}

AttendeeFormSet = _formset(Booking, BookingAttendee, ("email", "name", "receive_emails"))
WindowFormSet = _formset(EventType, AvailabilityWindow, ("weekday", "start_time", "end_time"), TIME)
OverrideFormSet = _formset(EventType, DateOverride, ("date", "is_unavailable", "start_time", "end_time"),
                           {"date": forms.DateInput(attrs={"type": "date"}, format="%Y-%m-%d"), **TIME})
FixedAttendeeFormSet = _formset(EventType, FixedAttendee, ("email", "name", "receive_emails"))
TypeQuestionFormSet = _formset(EventType, EventQuestion, ("sort_order", "key", "label", "kind", "options", "required"),
                               _textareas(2, "options"))
TypeTranslationFormSet = _formset(EventType, EventTypeTranslation,
                                  ("language", "name", "tagline", "price", "description", "highlights",
                                   "confirmation_note"),
                                  _textareas(3, "description", "highlights", "confirmation_note"))
QuestionTranslationFormSet = _formset(EventQuestion, EventQuestionTranslation, ("language", "label", "help_text", "options"),
                                      _textareas(3, "options"))
PaymentTranslationFormSet = _formset(PaymentMethod, PaymentMethodTranslation, ("language", "name", "amount_text", "details", "note"),
                                     _textareas(3, "details"))
