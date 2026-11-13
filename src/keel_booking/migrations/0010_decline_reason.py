from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("keel_booking", "0009_mail_delivered"),
    ]

    operations = [
        migrations.AddField(
            model_name="booking",
            name="decline_reason",
            field=models.CharField(blank=True, choices=[("cancel", "Cancelled"), ("reschedule", "Asked to pick a new time")], help_text="Why a request was declined: cancelled, or the guest was asked to pick a new time.", max_length=12),
        ),
    ]
