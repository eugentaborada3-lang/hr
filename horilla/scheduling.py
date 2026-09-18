"""Central registration for recurring Horilla jobs.

Only the ``run_scheduler`` management command may create an APScheduler
instance.  Application imports and Gunicorn workers import job functions only.
"""

import logging
from importlib import import_module

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.executors.pool import ThreadPoolExecutor
from django.apps import apps

logger = logging.getLogger(__name__)

DEFAULT_JOB_OPTIONS = {
    "replace_existing": True,
    "max_instances": 1,
    "coalesce": True,
    "misfire_grace_time": 3600,
}


def add_job(scheduler, func, trigger, *, job_id, **kwargs):
    """Register a uniquely named, single-instance recurring job."""
    options = {**DEFAULT_JOB_OPTIONS, **kwargs, "id": job_id, "name": job_id}
    # replace_existing alone does not remove duplicate pending jobs before start.
    if scheduler.get_job(job_id):
        scheduler.remove_job(job_id)
    from horilla.scheduler_runtime import execute_job
    call_kwargs = options.pop("kwargs", {})
    scheduler.add_job(execute_job, trigger, kwargs={"func": func, "call_kwargs": call_kwargs}, **options)
    logger.info("Registered scheduler job %s", job_id)


def build_scheduler():
    """Build, but do not start, the single process-wide scheduler."""
    from django.conf import settings

    return BackgroundScheduler(timezone=settings.TIME_ZONE, executors={"default": ThreadPoolExecutor(1)})


def register_all_jobs(scheduler):
    """Register every recurring job shipped by Horilla exactly once."""
    from base import scheduler as base
    from attendance import scheduler as attendance
    from employee import scheduler as employee
    from leave import scheduler as leave
    from payroll import scheduler as payroll
    from recruitment import scheduler as recruitment
    from pms import scheduler as pms
    from asset import scheduler as asset
    from report import scheduler as report

    jobs = (
        (base.rotate_shift, "interval", "base.rotate_shift", {"hours": 4}),
        (base.rotate_work_type, "interval", "base.rotate_work_type", {"hours": 4}),
        (base.undo_shift, "interval", "base.undo_shift", {"hours": 4}),
        (base.switch_shift, "interval", "base.switch_shift", {"hours": 4}),
        (base.undo_work_type, "interval", "base.undo_work_type", {"hours": 4}),
        (base.switch_work_type, "interval", "base.switch_work_type", {"hours": 4}),
        (base.recurring_holiday, "interval", "base.recurring_holiday", {"hours": 4}),
        (base.sync_roster_shifts, "interval", "base.sync_roster_shifts", {"hours": 4}),
        (attendance.create_work_record, "interval", "attendance.work_records", {"minutes": 30, "misfire_grace_time": 10800}),
        (attendance.create_work_record, "cron", "attendance.daily_work_records", {"hour": 0, "minute": 30, "misfire_grace_time": 32400}),
        (attendance.auto_punch_out, "interval", "attendance.auto_punch_out", {"minutes": 5, "misfire_grace_time": 600}),
        (attendance.send_check_in_reminders, "cron", "attendance.check_in_reminders", {"hour": 8, "minute": 30}),
        (attendance.send_missing_checkout_reminders, "cron", "attendance.missing_checkout_reminders", {"hour": 18, "minute": 0}),
        (employee.update_experience, "interval", "employee.update_experience", {"hours": 4}),
        (employee.block_unblock_disciplinary, "interval", "employee.disciplinary_accounts", {"seconds": 60}),
        (leave.leave_reset, "interval", "leave.reset", {"hours": 4}),
        (payroll.expire_contract, "interval", "payroll.expire_contract", {"hours": 4}),
        (payroll.auto_payslip_generate, "interval", "payroll.auto_payslips", {"hours": 3}),
        (recruitment.candidate_convert, "interval", "recruitment.candidate_convert", {"minutes": 5}),
        (recruitment.recruitment_close, "interval", "recruitment.close", {"hours": 1}),
        (pms.cyclic_feedback_creation, "cron", "pms.cyclic_feedback", {"hour": 8, "misfire_grace_time": 86400}),
        (asset.notify_expiring_assets, "interval", "asset.expiring_assets", {"days": 1}),
        (asset.notify_expiring_documents, "interval", "asset.expiring_documents", {"hours": 4}),
        (asset.mark_expired_assets, "interval", "asset.mark_expired", {"days": 1}),
        (report.run_report_subscriptions, "interval", "report.subscriptions", {"hours": 1}),
    )
    for func, trigger, job_id, kwargs in jobs:
        add_job(scheduler, func, trigger, job_id=job_id, **kwargs)

    if apps.is_installed("outlook_auth"):
        from outlook_auth.scheduler import refresh_outlook_auth_token
        add_job(scheduler, refresh_outlook_auth_token, "interval", job_id="outlook.refresh_tokens", minutes=50)
    if apps.is_installed("pg_backup"):
        from pg_backup.scheduler import BACKUP_CRON_TIMES, backup_postgres
        for value in filter(None, (t.strip() for t in BACKUP_CRON_TIMES.split(","))):
            hour, minute = map(int, value.split(":"))
            add_job(scheduler, backup_postgres, "cron", job_id=f"pg_backup.{hour:02d}:{minute:02d}", hour=hour, minute=minute)


def reconcile_dynamic_jobs(scheduler):
    """Refresh changed schedules without resetting unchanged interval timers."""
    desired = {}
    if apps.is_installed("biometric"):
        from biometric.models import BiometricDevices
        from biometric.views import str_time_seconds
        for device in BiometricDevices.objects.filter(is_scheduler=True):
            try:
                seconds = str_time_seconds(device.scheduler_duration)
                if seconds <= 0 or device.machine_type not in {"zk", "anviz", "dahua", "cosec", "etimeoffice"}:
                    raise ValueError("unsupported device or non-positive interval")
                name = f"{device.machine_type}_biometric_attendance_scheduler"
                func = getattr(import_module("biometric.views"), name)
                desired[f"biometric.{device.pk}"] = (func, "interval", {"seconds": seconds}, {"device_id": str(device.pk)})
            except (TypeError, ValueError):
                logger.exception("Skipping invalid biometric schedule %s", device.pk)
    if apps.is_installed("horilla_backup"):
        from horilla_backup.models import GoogleDriveBackup
        from horilla_backup.scheduler import google_drive_backup
        backup = GoogleDriveBackup.objects.filter(active=True).first()
        if backup:
            if backup.interval:
                if not backup.seconds or backup.seconds <= 0:
                    raise ValueError("Active Google Drive backup needs a positive interval")
                trigger, options = "interval", {"seconds": backup.seconds}
            else:
                trigger, options = "cron", {"hour": backup.hour, "minute": backup.minute}
                if backup.hour is None or backup.minute is None:
                    raise ValueError("Active Google Drive backup needs hour and minute")
            desired["backup.gdrive"] = (google_drive_backup, trigger, options, {})
    signatures = getattr(scheduler, "_horilla_dynamic_signatures", {})
    for job_id in signatures.keys() - desired.keys():
        if scheduler.get_job(job_id):
            scheduler.remove_job(job_id)
        logger.info("Removed disabled schedule %s", job_id)
    updated = {}
    for job_id, (func, trigger, options, call_kwargs) in desired.items():
        signature = (func.__name__, trigger, options, call_kwargs)
        if signatures.get(job_id) != signature:
            add_job(scheduler, func, trigger, job_id=job_id, kwargs=call_kwargs, **options)
        updated[job_id] = signature
    scheduler._horilla_dynamic_signatures = updated
