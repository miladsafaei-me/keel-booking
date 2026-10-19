"""Run the booking tick every minute, forever. One small container runs this beside the web one."""

import logging
import signal
import time

from django.core.management.base import BaseCommand
from django.db import close_old_connections

from keel_booking import services

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
        self.stdout.write(f"booking worker: one pass every {interval}s")
        while running["on"]:
            close_old_connections()
            try:
                done = services.tick()
                if any(done.values()):
                    self.stdout.write(f"booking worker: {done}")
            except Exception:
                log.exception("booking tick failed")
            for _ in range(interval):
                if not running["on"]:
                    break
                time.sleep(1)
