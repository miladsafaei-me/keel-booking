from django.contrib import admin
from django.urls import include, path

urlpatterns = [
    path("book/admin/", admin.site.urls),
    path("book/", include("keel_booking.urls")),
]
