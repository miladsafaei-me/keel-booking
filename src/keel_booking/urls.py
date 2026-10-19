"""Mount under the booking root, for example ``path("book/", include("keel_booking.urls"))``.

The admin is mounted separately by the host (the package's admin pages, the Google connect
flow included, live inside Django's admin).
"""

from django.urls import path

from . import views

app_name = "keel_booking"

urlpatterns = [
    path("", views.landing, name="landing"),
    path("confirmed/<uuid:uid>/", views.confirmed, name="confirmed"),
    path("<slug:slug>/", views.event_type_page, name="event_type"),
    path("<slug:slug>/slots/", views.slots, name="slots"),
    path("<slug:slug>/reserve/", views.reserve, name="reserve"),
]
