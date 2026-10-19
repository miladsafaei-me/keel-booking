SECRET_KEY = "test"
USE_TZ = True
LANGUAGE_CODE = "en"
LANGUAGES = [("en", "English"), ("tr", "Türkçe"), ("fa", "فارسی")]
TIME_ZONE = "UTC"
INSTALLED_APPS = [
    "django.contrib.admin", "django.contrib.auth", "django.contrib.contenttypes",
    "django.contrib.sessions", "django.contrib.messages", "django.contrib.staticfiles", "keel_booking",
]
MIDDLEWARE = [
    "django.contrib.sessions.middleware.SessionMiddleware", "django.middleware.locale.LocaleMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware", "django.contrib.messages.middleware.MessageMiddleware",
]
DATABASES = {"default": {"ENGINE": "django.db.backends.sqlite3", "NAME": ":memory:"}}
ROOT_URLCONF = "tests.urls"
STATIC_URL = "/static/"
TEMPLATES = [{"BACKEND": "django.template.backends.django.DjangoTemplates", "APP_DIRS": True,
              "OPTIONS": {"context_processors": ["django.template.context_processors.request",
                                                 "django.contrib.auth.context_processors.auth",
                                                 "django.contrib.messages.context_processors.messages"]}}]
EMAIL_BACKEND = "django.core.mail.backends.locmem.EmailBackend"
KEEL_BOOKING = {"OWNER_NAME": "Test Owner", "OWNER_EMAIL": "owner@example.com", "OWNER_TIMEZONE": "Europe/Istanbul",
                "SITE_URL": "https://example.com"}
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"
