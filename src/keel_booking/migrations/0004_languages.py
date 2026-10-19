import django.db.models.deletion
from django.db import migrations, models

import keel_booking.models


class Migration(migrations.Migration):
    dependencies = [("keel_booking", "0003_meet_access")]

    operations = [
        migrations.AddField(
            model_name="booking",
            name="language",
            field=models.CharField(blank=True, help_text="The language the guest booked in; their emails are written in it.",
                                   max_length=10),
        ),
        migrations.CreateModel(
            name="EventTypeTranslation",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("language", models.CharField(choices=keel_booking.models.language_choices, max_length=10)),
                ("name", models.CharField(blank=True, max_length=120)),
                ("tagline", models.CharField(blank=True, max_length=200)),
                ("description", models.TextField(blank=True)),
                ("highlights", models.TextField(blank=True, help_text="One point per line.")),
                ("confirmation_note", models.TextField(blank=True)),
                ("event_type", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="translations",
                                                 to="keel_booking.eventtype")),
            ],
            options={"verbose_name": "translation", "db_table": "keel_booking_event_type_translation",
                     "ordering": ("language",), "unique_together": {("event_type", "language")}},
        ),
    ]
