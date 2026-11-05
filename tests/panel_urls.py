from django.conf.urls.i18n import i18n_patterns
from django.contrib.auth import views as auth_views
from django.urls import include, path

from keel_booking.panel import views as panel_views

urlpatterns = [
    path("panel/login/", auth_views.LoginView.as_view(), name="login"),
    path("panel/bookings/", include("keel_booking.panel.urls")),
    # An older registered redirect URI kept as an alias.
    path("old/oauth/callback/", panel_views.google_callback, name="legacy_google_callback"),
] + i18n_patterns(path("book/", include("keel_booking.urls")), prefix_default_language=False)


def nav(request):
    from keel_booking.panel.nav import booking_section

    return [booking_section(request)]
