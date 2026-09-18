"""The only supported recurring-scheduler entrypoint."""
import logging
import os
import signal
import threading
import time
import uuid

from apscheduler.events import EVENT_JOB_ERROR, EVENT_JOB_MAX_INSTANCES, EVENT_JOB_MISSED
from django.core.cache import cache
from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from horilla.scheduler_runtime import HEARTBEAT_KEY, leadership, scheduler_leader
from horilla.scheduling import build_scheduler, reconcile_dynamic_jobs, register_all_jobs

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = "Run recurring HR jobs in one dedicated PostgreSQL-locked process."

    def add_arguments(self, parser):
        parser.add_argument("--dry-run", action="store_true", help="List jobs; do not start or execute them.")
        parser.add_argument("--probe", type=int, metavar="SECONDS", help="Hold leadership with scheduler PAUSED, then exit; no HR jobs run.")

    def handle(self, *args, **options):
        logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
        if options["probe"] is not None and options["probe"] <= 0:
            raise CommandError("--probe must be positive")
        scheduler = build_scheduler()
        stop = threading.Event()
        old_handlers = {}
        token = uuid.uuid4().hex
        try:
            register_all_jobs(scheduler)
            reconcile_dynamic_jobs(scheduler)
            for job in scheduler.get_jobs():
                self.stdout.write(f"{job.id}: {job.trigger}")
            if options["dry_run"]:
                self.stdout.write("Dry-run complete: no scheduler started and no jobs executed.")
                return
            if "redis" not in settings.CACHES["default"]["BACKEND"].lower():
                raise CommandError("Set REDIS_URL: scheduler readiness requires a shared Redis cache.")
            with scheduler_leader() as leader:
                for sig in (signal.SIGTERM, signal.SIGINT):
                    old_handlers[sig] = signal.signal(sig, lambda *_: stop.set())
                scheduler.add_listener(
                    lambda event: logger.warning("Scheduler event code=%s job=%s", event.code, event.job_id),
                    EVENT_JOB_ERROR | EVENT_JOB_MAX_INSTANCES | EVENT_JOB_MISSED,
                )
                probing = options["probe"] is not None
                deadline = time.monotonic() + options["probe"] if probing else None
                try:
                    scheduler.start(paused=probing)
                    logger.info("Dedicated scheduler started: pid=%s paused=%s jobs=%s", os.getpid(), probing, len(scheduler.get_jobs()))
                    while not stop.is_set():
                        # A lost session fails closed; never reacquire leadership
                        # while an old job could still be executing.
                        with leader.cursor() as cursor:
                            cursor.execute("SELECT 1")
                            cursor.fetchone()
                        if not probing:
                            reconcile_dynamic_jobs(scheduler)
                        cache.set(HEARTBEAT_KEY, {"token": token, "time": time.time(), "paused": probing}, timeout=90)
                        if deadline and time.monotonic() >= deadline:
                            break
                        stop.wait(min(15, max(0, deadline - time.monotonic())) if deadline else 15)
                finally:
                    leadership.clear()
                    # Keep the leadership session until in-flight work finishes.
                    if scheduler.running:
                        scheduler.shutdown(wait=True)
                    heartbeat = cache.get(HEARTBEAT_KEY)
                    if heartbeat and heartbeat.get("token") == token:
                        cache.delete(HEARTBEAT_KEY)
        finally:
            for sig, handler in old_handlers.items():
                signal.signal(sig, handler)
