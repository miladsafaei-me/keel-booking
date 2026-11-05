"""The owner's side of keel-booking inside the keel-web staff panel (needs keel-web 0.10 or later).

Mount it in the panel's URLconf with ``include("keel_booking.panel.urls")`` and place
``keel_booking.panel.nav.booking_section`` in the host's sidebar. Once mounted, the owner's
emails and Telegram messages link here instead of the Django admin. The Django admin
(``keel_booking.admin``) keeps working for a host that does not mount the panel.
"""
