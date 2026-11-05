from django.urls import path

from . import views

app_name = "keel_booking_panel"

urlpatterns = [
    path("", views.booking_list, name="bookings"),
    path("<int:pk>/", views.booking_detail, name="booking"),
    path("file/<int:pk>/", views.booking_file, name="booking_file"),
    path("follow-ups/", views.follow_up_list, name="follow_ups"),
    path("types/", views.event_type_list, name="event_types"),
    path("types/new/", views.event_type_edit, name="event_type_new"),
    path("types/<int:pk>/", views.event_type_edit, name="event_type"),
    path("questions/<int:pk>/", views.question_edit, name="question"),
    path("payments/", views.payment_method_list, name="payment_methods"),
    path("payments/new/", views.payment_method_edit, name="payment_method_new"),
    path("payments/<int:pk>/", views.payment_method_edit, name="payment_method"),
    path("google/", views.google_page, name="google"),
    path("google/connect/", views.google_connect, name="google_connect"),
    path("google/callback/", views.google_callback, name="google_callback"),
    path("google/disconnect/", views.google_disconnect, name="google_disconnect"),
]
