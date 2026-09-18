import time

from django.core.cache import cache
from django.core.management.base import BaseCommand, CommandError

from horilla.scheduler_runtime import HEARTBEAT_KEY


class Command(BaseCommand):
    help = "Check the shared Redis scheduler heartbeat. Paused probes are not ready."

    def handle(self, *args, **options):
        heartbeat = cache.get(HEARTBEAT_KEY)
        if not isinstance(heartbeat, dict) or heartbeat.get("paused") or time.time() - heartbeat.get("time", 0) > 60:
            raise CommandError("Scheduler heartbeat is stale, paused, or absent.")
        self.stdout.write("Scheduler is healthy.")
