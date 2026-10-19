"""The owner's side: meeting types and their rules, bookings with notes and follow-ups, and the Google connection."""

from __future__ import annotations

from django.contrib import admin, messages
from django.http import FileResponse, Http404, HttpResponseRedirect
from django.shortcuts import get_object_or_404, redirect
from django.urls import path, reverse
from django.utils import timezone
from django.utils.html import format_html, format_html_join

from . import google, services
from .models import (AvailabilityWindow, Booking, BookingAttendee, BookingFile, BookingNote, DateOverride, EventType,
                     FixedAttendee, FollowUp, GoogleAccount)


class AvailabilityWindowInline(admin.TabularInline):
    model = AvailabilityWindow
    extra = 0


class DateOverrideInline(admin.TabularInline):
    model = DateOverride
    extra = 0


class FixedAttendeeInline(admin.TabularInline):
    model = FixedAttendee
    extra = 0


@admin.register(EventType)
class EventTypeAdmin(admin.ModelAdmin):
    list_display = ("name", "slug", "duration_minutes", "timezone", "is_active", "sort_order", "page_link")
    list_editable = ("is_active", "sort_order")
    prepopulated_fields = {"slug": ("name",)}
    inlines = (AvailabilityWindowInline, DateOverrideInline, FixedAttendeeInline)
    fieldsets = (
        ("Page", {"fields": ("name", "slug", "tagline", "description", "highlights", "icon", "is_active", "sort_order")}),
        ("Length and spacing", {"fields": ("duration_minutes", "buffer_before_minutes", "buffer_after_minutes",
                                           "slot_step_minutes", "daily_limit")}),
        ("Booking window", {"fields": ("timezone", "detect_visitor_timezone", "min_notice_minutes", "max_days_ahead")}),
        ("Meeting and emails", {"fields": ("create_meet_link", "allow_additional_guests", "reminder_minutes",
                                           "confirmation_note")}),
    )

    @admin.display(description="Page")
    def page_link(self, obj):
        url = reverse("keel_booking:event_type", args=[obj.slug])
        return format_html('<a href="{}" target="_blank" rel="noopener">{}</a>', url, url)


class BookingAttendeeInline(admin.TabularInline):
    model = BookingAttendee
    extra = 0
    fields = ("email", "name", "role", "receive_emails")
    readonly_fields = ("role",)


class BookingFileInline(admin.TabularInline):
    model = BookingFile
    extra = 0
    fields = ("download", "size_label", "content_type", "created_at")
    readonly_fields = fields
    can_delete = True

    def has_add_permission(self, request, obj=None):
        return False

    @admin.display(description="File")
    def download(self, obj):
        url = reverse("admin:keel_booking_booking_file", args=[obj.pk])
        return format_html('<a href="{}">{}</a>', url, obj.original_name)

    @admin.display(description="Size")
    def size_label(self, obj):
        return f"{obj.size / 1048576:.1f} MB" if obj.size >= 1048576 else f"{max(1, obj.size // 1024)} KB"


class BookingNoteInline(admin.StackedInline):
    model = BookingNote
    extra = 1
    fields = ("body", "created_at")
    readonly_fields = ("created_at",)


class FollowUpInline(admin.TabularInline):
    model = FollowUp
    extra = 1
    fields = ("due_at", "note", "is_done", "reminder_sent_at")
    readonly_fields = ("reminder_sent_at",)


class UpcomingFilter(admin.SimpleListFilter):
    title = "when"
    parameter_name = "when"

    def lookups(self, request, model_admin):
        return (("upcoming", "Upcoming"), ("past", "Past"))

    def queryset(self, request, queryset):
        now = timezone.now()
        if self.value() == "upcoming":
            return queryset.filter(end_at__gt=now).order_by("start_at")
        if self.value() == "past":
            return queryset.filter(end_at__lte=now)
        return queryset


@admin.register(Booking)
class BookingAdmin(admin.ModelAdmin):
    list_display = ("start_at", "event_type", "guest_name", "guest_company", "guest_email", "guest_phone",
                    "socials_short", "description_short", "file_count", "status", "meet", "next_follow_up")
    list_display_links = ("start_at", "guest_name")
    list_filter = (UpcomingFilter, "status", "event_type", "source")
    search_fields = ("guest_name", "guest_email", "guest_company", "guest_phone", "guest_message")
    date_hierarchy = "start_at"
    list_per_page = 50
    inlines = (BookingFileInline, BookingNoteInline, FollowUpInline, BookingAttendeeInline)
    readonly_fields = ("uid", "event_type", "start_at", "end_at", "status", "source", "meet_access", "guest_timezone", "meet", "calendar",
                       "socials_list", "confirmation_sent_at", "owner_notified_at", "reminder_sent_at", "sync_error",
                       "mail_error", "created_at")
    fieldsets = (
        ("Meeting", {"fields": ("event_type", "start_at", "end_at", "status", "source", "meet", "calendar")}),
        ("Guest", {"fields": ("guest_name", "guest_company", "guest_email", "guest_phone", "socials_list",
                              "guest_message", "guest_timezone")}),
        ("Delivery", {"classes": ("collapse",), "fields": ("meet_access", "confirmation_sent_at", "owner_notified_at", "reminder_sent_at",
                                                          "sync_error", "mail_error", "uid", "created_at")}),
    )
    actions = ("resync_to_google", "push_guests", "resend_confirmation", "cancel_bookings")

    def has_add_permission(self, request):
        return False

    def save_related(self, request, form, formsets, change):
        """After an edit, the calendar event's guest list follows the booking's attendees."""
        super().save_related(request, form, formsets, change)
        booking = form.instance
        services.ensure_guest_attendee(booking)
        if booking.google_event_id and booking.status == Booking.Status.CONFIRMED:
            if services.push_attendees(booking):
                self.message_user(request, "The calendar event's guests now match this booking, so each can enter the Meet.")
            else:
                self.message_user(request, "The calendar event could not be updated; see Delivery for the error.", messages.WARNING)

    def get_queryset(self, request):
        from django.db.models import Count

        return super().get_queryset(request).select_related("event_type").annotate(files_total=Count("files"))

    def get_urls(self):
        own = [path("file/<int:pk>/", self.admin_site.admin_view(self.file_view), name="keel_booking_booking_file")]
        return own + super().get_urls()

    def file_view(self, request, pk):
        """A guest's attachment, to staff only: the files are never on a public URL."""
        if not self.has_view_permission(request):
            raise Http404
        item = get_object_or_404(BookingFile, pk=pk)
        try:
            handle = item.file.open("rb")
        except FileNotFoundError:
            raise Http404
        return FileResponse(handle, as_attachment=True, filename=item.original_name)

    @admin.display(description="Social")
    def socials_short(self, obj):
        return ", ".join(f"{name}: {handle}" for name, handle in obj.socials_display()) or "-"

    @admin.display(description="Social profiles")
    def socials_list(self, obj):
        rows = obj.socials_display()
        if not rows:
            return "-"
        return format_html_join("", "<div>{}: {}</div>", rows)

    @admin.display(description="Description")
    def description_short(self, obj):
        text = obj.guest_message or ""
        return (text[:80] + "...") if len(text) > 80 else (text or "-")

    @admin.display(description="Files", ordering="files_total")
    def file_count(self, obj):
        return obj.files_total or "-"

    @admin.display(description="Google Meet")
    def meet(self, obj):
        if not obj.meet_url:
            return "-"
        return format_html('<a href="{}" target="_blank" rel="noopener">{}</a>', obj.meet_url, obj.meet_url)

    @admin.display(description="Calendar event")
    def calendar(self, obj):
        if not obj.google_event_link:
            return "Not on the calendar yet" if obj.status == Booking.Status.CONFIRMED else "-"
        return format_html('<a href="{}" target="_blank" rel="noopener">Open in Google Calendar</a>', obj.google_event_link)

    @admin.display(description="Next follow-up")
    def next_follow_up(self, obj):
        follow_up = obj.follow_ups.filter(is_done=False).order_by("due_at").first()
        return timezone.localtime(follow_up.due_at).strftime("%Y-%m-%d %H:%M") if follow_up else "-"

    @admin.action(description="Write to Google Calendar again")
    def resync_to_google(self, request, queryset):
        written = sum(1 for booking in queryset.filter(status=Booking.Status.CONFIRMED) if services.sync_to_google(booking))
        self.message_user(request, f"{written} of {queryset.count()} meetings are on the calendar.")

    @admin.action(description="Copy the guests to the calendar event (lets them into the Meet)")
    def push_guests(self, request, queryset):
        done = 0
        for booking in queryset.filter(status=Booking.Status.CONFIRMED):
            services.ensure_guest_attendee(booking)
            done += services.push_attendees(booking)
            services.apply_meet_access(booking)
        self.message_user(request, f"{done} calendar events updated.")

    @admin.action(description="Send the confirmation email again")
    def resend_confirmation(self, request, queryset):
        for booking in queryset.filter(status=Booking.Status.CONFIRMED):
            booking.confirmation_sent_at = None
            booking.mail_attempts = 0
            booking.save(update_fields=["confirmation_sent_at", "mail_attempts"])
            services.send_new_booking_emails(booking)
        self.message_user(request, "Confirmations sent again.")

    @admin.action(description="Cancel, remove from the calendar and tell the guests")
    def cancel_bookings(self, request, queryset):
        for booking in queryset:
            services.cancel_booking(booking)
        self.message_user(request, f"{queryset.count()} meetings cancelled.")


@admin.register(FollowUp)
class FollowUpAdmin(admin.ModelAdmin):
    list_display = ("due_at", "booking", "note", "is_done", "reminder_sent_at")
    list_filter = ("is_done",)
    list_editable = ("is_done",)
    raw_id_fields = ("booking",)


@admin.register(GoogleAccount)
class GoogleAccountAdmin(admin.ModelAdmin):
    """One row. Its page connects or disconnects the Google account and picks the calendars."""

    change_form_template = "admin/keel_booking/googleaccount/change_form.html"
    fields = ("email", "calendar_id", "busy_calendar_ids", "connected_at", "last_error")
    readonly_fields = ("email", "connected_at", "last_error")

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False

    def changelist_view(self, request, extra_context=None):
        account = GoogleAccount.load()
        return redirect("admin:keel_booking_googleaccount_change", account.pk)

    def get_urls(self):
        own = [
            path("oauth/start/", self.admin_site.admin_view(self.oauth_start), name="keel_booking_google_start"),
            path("oauth/callback/", self.admin_site.admin_view(self.oauth_callback), name="keel_booking_google_callback"),
            path("oauth/disconnect/", self.admin_site.admin_view(self.oauth_disconnect), name="keel_booking_google_disconnect"),
        ]
        return own + super().get_urls()

    def _redirect_uri(self, request) -> str:
        from . import conf

        path_ = reverse("admin:keel_booking_google_callback")
        return conf.absolute(path_) if conf.get("SITE_URL") else request.build_absolute_uri(path_)

    def change_view(self, request, object_id, form_url="", extra_context=None):
        extra_context = {**(extra_context or {}), "google_configured": google.is_configured(),
                         "redirect_uri": self._redirect_uri(request), "account": GoogleAccount.load(),
                         "scopes": google.scopes()}
        return super().change_view(request, object_id, form_url, extra_context)

    def oauth_start(self, request):
        if not google.is_configured():
            messages.error(request, "Set GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET in KEEL_BOOKING first.")
            return redirect("admin:keel_booking_googleaccount_changelist")
        url, state = google.authorization_url(self._redirect_uri(request))
        request.session["keel_booking_oauth_state"] = state
        return HttpResponseRedirect(url)

    def oauth_callback(self, request):
        expected = request.session.pop("keel_booking_oauth_state", None)
        if not expected or request.GET.get("state") != expected:
            messages.error(request, "The Google answer did not match this session. Please connect again.")
        elif request.GET.get("error"):
            messages.error(request, f"Google said: {request.GET['error']}")
        else:
            try:
                account = google.exchange_code(request.GET.get("code", ""), self._redirect_uri(request))
                messages.success(request, f"Connected {account.email}. New meetings now go on its calendar with a Meet link.")
            except google.GoogleError as error:
                messages.error(request, str(error))
        return redirect("admin:keel_booking_googleaccount_changelist")

    def oauth_disconnect(self, request):
        if request.method == "POST":
            google.disconnect()
            messages.success(request, "Google is disconnected.")
        return redirect("admin:keel_booking_googleaccount_changelist")
