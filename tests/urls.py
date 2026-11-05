from django.conf.urls.i18n import i18n_patterns
from django.contrib import admin
from django.urls import include, path

from keel_booking.views import assistant_chat

urlpatterns = [path("book/admin/", admin.site.urls)] + i18n_patterns(
    path("book/", include("keel_booking.urls")), path("booking/chat", assistant_chat, name="assistant"),
    prefix_default_language=False)
