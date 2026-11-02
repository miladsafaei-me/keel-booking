"""Run the booking tick every minute, forever. One small container runs this beside the web one.

When the owner's Telegram bot is set up, the wait between ticks is spent long-polling it, so an
Approve or Decline press is handled within seconds. Run one worker per bot: Telegram gives each
update to one poller only.
"""

import logging
import signal
import time

from django.core.management.base import BaseCommand
from django.db import close_old_connections

from keel_booking import services, telegram

log = logging.getLogger(__name__)


class Command(BaseCommand):
    help = "Run booking_tick every --interval seconds until stopped."

    def add_arguments(self, parser):
        parser.add_argument("--interval", type=int, default=60)

    def handle(self, *args, interval, **options):
        running = {"on": True}

        def stop(*_):
            running["on"] = False

        signal.signal(signal.SIGTERM, stop)
        signal.signal(signal.SIGINT, stop)
        self.stdout.write(f"booking worker: one pass every {interval}s"
                          + (", Telegram requests on" if telegram.is_configured() else ""))
        offset = None
        while running["on"]:
            close_old_connections()
            try:
                done = services.tick()
                if any(done.values()):
                    self.stdout.write(f"booking worker: {done}")
            except Exception:
                log.exception("booking tick failed")
            deadline = time.monotonic() + interval
            while running["on"] and time.monotonic() < deadline:
                if telegram.conf.get("TELEGRAM_BOT_TOKEN"):
                    try:
                        offset = telegram.poll(offset, timeout=max(1, min(10, int(deadline - time.monotonic()))))
                        close_old_connections()
                        continue
                    except telegram.TelegramError as error:
                        log.warning("Telegram poll failed: %s", error)
                        time.sleep(5)
                        continue
                time.sleep(1)
