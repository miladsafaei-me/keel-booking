from django.core.management.base import BaseCommand

from keel_booking import services


class Command(BaseCommand):
    help = "Run one pass of the booking background work: calendar retries, emails, reminders, follow-ups."

    def handle(self, *args, **options):
        self.stdout.write(str(services.tick()))
