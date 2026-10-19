from django.conf.urls.i18n import i18n_patterns
from django.contrib import admin
from django.urls import include, path

urlpatterns = [path("book/admin/", admin.site.urls)] + i18n_patterns(
    path("book/", include("keel_booking.urls")), prefix_default_language=False)
