"""Safe regression tests: mocks for HR effects, real APScheduler registration."""
import ast
import io
import os
import subprocess
import sys
from datetime import date, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, Mock, patch

from django.conf import settings
from django.core.management import call_command, get_commands
from django.core.management.base import CommandError
from django.test import SimpleTestCase

from horilla.scheduler_runtime import execute_job, leadership, scheduler_leader
from horilla.scheduling import add_job, build_scheduler, reconcile_dynamic_jobs, register_all_jobs


EXPECTED = {
    "base.rotate_shift", "base.rotate_work_type", "base.undo_shift", "base.switch_shift",
    "base.undo_work_type", "base.switch_work_type", "base.recurring_holiday", "base.sync_roster_shifts",
    "attendance.work_records", "attendance.daily_work_records", "attendance.auto_punch_out",
    "attendance.check_in_reminders", "attendance.missing_checkout_reminders",
    "employee.update_experience", "employee.disciplinary_accounts", "leave.reset",
    "payroll.expire_contract", "payroll.auto_payslips", "recruitment.candidate_convert",
    "recruitment.close", "pms.cyclic_feedback", "asset.expiring_assets",
    "asset.expiring_documents", "asset.mark_expired", "report.subscriptions",
}


class RegistrationTests(SimpleTestCase):
    def test_commands_are_discoverable(self):
        self.assertEqual(get_commands()["run_scheduler"], "base")
        self.assertEqual(get_commands()["scheduler_health"], "base")

    def test_all_core_jobs_registered_once_even_before_start(self):
        scheduler = build_scheduler()
        register_all_jobs(scheduler)
        register_all_jobs(scheduler)
        jobs = scheduler.get_jobs()
        self.assertEqual({j.id for j in jobs}, EXPECTED)
        self.assertEqual(len(jobs), len(EXPECTED))
        self.assertFalse(scheduler.running)
        for job in jobs:
            self.assertEqual(job.max_instances, 1)
            self.assertTrue(job.coalesce)
            self.assertGreater(job.misfire_grace_time, 0)
            self.assertIs(job.func, execute_job)

    def test_repeated_registration_after_start(self):
        scheduler = build_scheduler()
        try:
            scheduler.start(paused=True)
            register_all_jobs(scheduler)
            register_all_jobs(scheduler)
            self.assertEqual(len(scheduler.get_jobs()), len(EXPECTED))
        finally:
            scheduler.shutdown()

    def test_dry_run_does_not_start_or_acquire_leadership(self):
        with patch("base.management.commands.run_scheduler.reconcile_dynamic_jobs"), patch(
            "base.management.commands.run_scheduler.scheduler_leader"
        ) as leader, patch("apscheduler.schedulers.background.BackgroundScheduler.start") as start:
            out = io.StringIO()
            with patch("base.management.commands.run_scheduler.logging.basicConfig"):
                call_command("run_scheduler", dry_run=True, stdout=out)
            leader.assert_not_called()
            start.assert_not_called()
            self.assertIn("no jobs executed", out.getvalue())

    def test_dynamic_device_unchanged_changed_disabled(self):
        scheduler = build_scheduler()
        device = SimpleNamespace(pk="device-one", machine_type="zk", scheduler_duration="00:05")
        with patch("biometric.models.BiometricDevices.objects.filter", return_value=[device]) as devices, patch(
            "horilla_backup.models.GoogleDriveBackup.objects.filter"
        ) as backups:
            backups.return_value.first.return_value = None
            reconcile_dynamic_jobs(scheduler)
            original = scheduler.get_job("biometric.device-one")
            self.assertEqual(original.kwargs["call_kwargs"], {"device_id": "device-one"})
            reconcile_dynamic_jobs(scheduler)
            self.assertIs(scheduler.get_job("biometric.device-one"), original)
            device.scheduler_duration = "00:10"
            reconcile_dynamic_jobs(scheduler)
            self.assertIsNot(scheduler.get_job("biometric.device-one"), original)
            self.assertEqual(len(scheduler.get_jobs()), 1)
            devices.return_value = []
            reconcile_dynamic_jobs(scheduler)
            self.assertEqual(scheduler.get_jobs(), [])

    def test_backup_schedule_tracks_active_configuration(self):
        scheduler = build_scheduler()
        backup = SimpleNamespace(interval=True, seconds=300)
        with patch("biometric.models.BiometricDevices.objects.filter", return_value=[]), patch(
            "horilla_backup.models.GoogleDriveBackup.objects.filter"
        ) as backups:
            backups.return_value.first.return_value = backup
            reconcile_dynamic_jobs(scheduler)
            reconcile_dynamic_jobs(scheduler)
            self.assertEqual([j.id for j in scheduler.get_jobs()], ["backup.gdrive"])
            backups.return_value.first.return_value = None
            reconcile_dynamic_jobs(scheduler)
            self.assertEqual(scheduler.get_jobs(), [])

    def test_only_dedicated_command_starts_scheduler(self):
        root = Path(settings.BASE_DIR)
        allowed = {"horilla/scheduling.py", "base/management/commands/run_scheduler.py"}
        paths = []
        for directory, dirs, files in os.walk(root):
            dirs[:] = [d for d in dirs if d not in {"venv", ".venv", ".git", "node_modules", "staticfiles", "media", "__pycache__"}]
            paths.extend(str((Path(directory) / f).relative_to(root)) for f in files if f.endswith(".py"))
        for relative in paths:
            path = root / relative
            if not path.exists() or relative in allowed or "/tests/" in relative:
                continue
            tree = ast.parse(path.read_text(encoding="utf-8-sig"), filename=relative)
            for node in ast.walk(tree):
                if isinstance(node, ast.Call):
                    name = ast.unparse(node.func)
                    self.assertNotIn(name, {"BackgroundScheduler", "AsyncIOScheduler", "scheduler.start"}, relative)

    def test_cold_startup_wsgi_and_management_contexts_never_start_scheduler(self):
        script = '''
import os, sys, threading
from unittest.mock import patch
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "horilla.settings")
context = sys.argv[1]
sys.argv = ["manage.py", context]
with patch("apscheduler.schedulers.base.BaseScheduler.start", side_effect=AssertionError("UNEXPECTED SCHEDULER START")):
    import django
    django.setup()
    from django.core.wsgi import get_wsgi_application
    get_wsgi_application()
    from django.core.checks import run_checks
    run_checks()
    assert not any(t.name == "APScheduler" for t in threading.enumerate())
print("STARTUP_ISOLATED", context)
'''
        for context in ("gunicorn", "runserver", "migrate", "test", "collectstatic", "shell"):
            result = subprocess.run([sys.executable, "-c", script, context], capture_output=True, text=True, timeout=90)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn("STARTUP_ISOLATED", result.stdout)


class LockTests(SimpleTestCase):
    def tearDown(self):
        leadership.clear()

    def test_job_skips_without_leadership(self):
        job = Mock(__name__="test_job")
        execute_job(job)
        job.assert_not_called()

    def test_health_rejects_missing_stale_and_paused_heartbeat(self):
        import time
        for heartbeat in (None, {"time": time.time() - 120}, {"time": time.time(), "paused": True}):
            with patch("base.management.commands.scheduler_health.cache.get", return_value=heartbeat):
                with self.assertRaises(CommandError):
                    call_command("scheduler_health", stdout=io.StringIO())
        with patch("base.management.commands.scheduler_health.cache.get", return_value={"time": time.time(), "paused": False}):
            call_command("scheduler_health", stdout=io.StringIO())

    def test_competing_leader_is_rejected_and_connection_closed(self):
        with patch("horilla.scheduler_runtime.connection") as connection:
            connection.vendor = "postgresql"
            leader = connection.copy.return_value
            leader.cursor.return_value.__enter__.return_value.fetchone.return_value = (False,)
            with self.assertRaisesMessage(CommandError, "already holds"):
                with scheduler_leader():
                    self.fail("Acquired competing lock")
            leader.close.assert_called_once()
            self.assertFalse(leadership.is_set())

    def test_job_skips_if_old_job_still_holds_execution_lock(self):
        leadership.set()
        with patch("horilla.scheduler_runtime.close_old_connections"), patch(
            "horilla.scheduler_runtime.transaction.atomic"
        ), patch("horilla.scheduler_runtime.connection") as connection:
            connection.cursor.return_value.__enter__.return_value.fetchone.return_value = (False,)
            job = Mock(__name__="test_job")
            execute_job(job)
            job.assert_not_called()

    def test_job_failure_propagates_through_transaction(self):
        leadership.set()
        with patch("horilla.scheduler_runtime.close_old_connections"), patch(
            "horilla.scheduler_runtime.transaction.atomic"
        ) as atomic, patch("horilla.scheduler_runtime.connection") as connection:
            connection.cursor.return_value.__enter__.return_value.fetchone.return_value = (True,)
            job = Mock(__name__="test_job", side_effect=ValueError("failed"))
            with self.assertRaises(ValueError):
                execute_job(job)
            self.assertIs(atomic.return_value.__exit__.call_args.args[0], ValueError)


class IdempotencyTests(SimpleTestCase):
    def test_checkin_reminder_is_not_sent_twice(self):
        from attendance.scheduler import send_check_in_reminders
        employee = SimpleNamespace(employee_user_id="user", get_shift_schedule=lambda: object())
        with patch("attendance.scheduler.timezone.localdate", return_value=date(2026, 9, 18)), patch(
            "employee.models.Employee.objects.filter", return_value=[employee]
        ), patch("attendance.models.Attendance.objects.filter") as attendance, patch(
            "attendance.scheduler._notification_already_sent", side_effect=[False, True]
        ), patch("notifications.signals.notify.send") as send:
            attendance.return_value.exists.return_value = False
            send_check_in_reminders()
            send_check_in_reminders()
            send.assert_called_once()

    def test_mid_month_payslip_existing_record_is_not_recalculated(self):
        from payroll.scheduler import generate_payslip
        employee = SimpleNamespace(pk=123)
        with patch("employee.models.Employee.objects") as employees, patch(
            "payroll.scheduler.Contract.objects"
        ) as contracts, patch("payroll.scheduler.Payslip.objects") as payslips, patch(
            "payroll.scheduler.payroll_calculation"
        ) as calculate:
            employees.none.return_value.__or__.return_value.filter.return_value.distinct.return_value = [employee]
            contracts.filter.return_value.first.return_value = SimpleNamespace(contract_start_date=date(2026, 8, 15))
            payslips.filter.return_value.first.return_value = SimpleNamespace(pk=456)
            generate_payslip(date(2026, 9, 1), [], True)
            generate_payslip(date(2026, 9, 1), [], True)
            payslips.filter.assert_called_with(employee_id=employee, start_date=date(2026, 8, 15), end_date=date(2026, 8, 31))
            calculate.assert_not_called()

    def test_leave_reset_advances_marker_only_once(self):
        from leave.scheduler import leave_reset
        available = Mock(reset_date=date.today(), expired_date=None)
        available.set_reset_date.return_value = date.today() + timedelta(days=365)
        leave_type = Mock(carryforward_expire_date=None)
        leave_type.employee_available_leave.all.return_value = [available]
        with patch("leave.models.LeaveType.objects.filter", return_value=[leave_type]), patch(
            "leave.scheduler.pre_scheduler.send"
        ), patch("leave.scheduler.post_scheduler.send"):
            leave_reset()
            leave_reset()
            available.update_carryforward.assert_called_once()
            available.save.assert_called_once()

    def test_asset_notification_is_only_sent_once_per_expiry(self):
        from asset.scheduler import _notify_expiry_once
        with patch("notifications.models.Notification.objects.filter") as notifications, patch(
            "asset.scheduler.notify.send"
        ) as send:
            notifications.return_value.exists.side_effect = [False, True]
            for _ in range(2):
                _notify_expiry_once("bot", recipient="employee", expiry_key="asset:1:2026-09-30", verb="Expires soon")
            send.assert_called_once()

    def test_cyclic_feedback_is_not_created_twice(self):
        from pms.scheduler import cyclic_feedback_creation
        original = SimpleNamespace(cyclic_feedback=True, cyclic_next_start_date=date.today(), cyclic_next_end_date=date.today(), _meta=SimpleNamespace(fields=[]), save=Mock())
        with patch("pms.models.Feedback") as feedback:
            feedback.objects.filter.return_value = [original]
            feedback.return_value.review_cycle = "Annual"
            cyclic_feedback_creation()
            cyclic_feedback_creation()
            self.assertEqual(feedback.call_count, 1)
            original.save.assert_called_once()
