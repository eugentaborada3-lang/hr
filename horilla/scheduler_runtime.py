"""PostgreSQL leadership and transactional execution for recurring jobs."""
import logging
import threading
from contextlib import contextmanager

from django.core.management.base import CommandError
from django.db import close_old_connections, connection, transaction

logger = logging.getLogger(__name__)
LEADER_LOCK = 731924118
EXECUTION_LOCK = 731924119
HEARTBEAT_KEY = "horilla:scheduler:heartbeat"
leadership = threading.Event()


@contextmanager
def scheduler_leader():
    """Use a dedicated session; ORM reconnects must never silently drop our lock."""
    if connection.vendor != "postgresql":
        raise CommandError("run_scheduler requires PostgreSQL session advisory locks.")
    leader = connection.copy(alias="scheduler_leader")
    try:
        leader.ensure_connection()
        with leader.cursor() as cursor:
            cursor.execute("SELECT pg_try_advisory_lock(%s)", [LEADER_LOCK])
            if not cursor.fetchone()[0]:
                raise CommandError("Another scheduler already holds the leadership lock.")
            cursor.execute("SELECT pg_backend_pid()")
            backend_pid = cursor.fetchone()[0]
        leadership.set()
        logger.info("Scheduler leadership acquired (database pid=%s)", backend_pid)
        # Raw driver connection, never Django's reconnecting cursor wrapper.
        yield leader.connection
    finally:
        leadership.clear()
        leader.close()
        logger.info("Scheduler leadership released")


def execute_job(func, call_kwargs=None):
    """Serialize database effects across failover; roll back failed jobs.

    External email/device/Drive effects cannot be rolled back. Their delivery
    semantics are NOT exactly-once; domain-level deduplication is still needed.
    """
    if not leadership.is_set():
        logger.warning("Skipping %s: no scheduler leadership", func.__name__)
        return
    close_old_connections()
    try:
        with transaction.atomic():
            with connection.cursor() as cursor:
                cursor.execute("SELECT pg_try_advisory_xact_lock(%s)", [EXECUTION_LOCK])
                if not cursor.fetchone()[0]:
                    logger.warning("Skipping %s: previous scheduler job still running", func.__name__)
                    return
            if not leadership.is_set():
                return
            logger.info("Job started: %s", func.__name__)
            result = func(**(call_kwargs or {}))
        logger.info("Job completed: %s", func.__name__)
        return result
    except Exception:
        logger.exception("Job failed: %s", func.__name__)
        raise
    finally:
        close_old_connections()
