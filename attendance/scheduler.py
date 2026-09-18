import datetime
from datetime import timedelta

from django.utils import timezone

from base.backends import logger


def auto_punch_out():
    from attendance.methods.utils import Request
    from attendance.models import Attendance, AttendanceActivity
    from attendance.views.clock_in_out import clock_out
    from base.models import EmployeeShiftSchedule

    automatic_check_out_shifts = EmployeeShiftSchedule.objects.filter(
        is_auto_punch_out_enabled=True
    )

    for shift_schedule in automatic_check_out_shifts:
        activities = AttendanceActivity.objects.filter(
            shift_day=shift_schedule.day,
            clock_out_date=None,
            clock_out=None,
        ).order_by("-created_at")

        for activity in activities:
            attendance = Attendance.objects.filter(
                employee_id=activity.employee_id,
                attendance_clock_out=None,
                attendance_clock_out_date=None,
                shift_id=shift_schedule.shift_id,
                attendance_day=shift_schedule.day,
                attendance_date=activity.attendance_date,
            ).first()

            if attendance:
                date = activity.attendance_date
                if (
                    shift_schedule.is_night_shift
                    and shift_schedule.start_time
                    and shift_schedule.end_time
                    and shift_schedule.start_time > shift_schedule.end_time
                ):
                    date += timedelta(days=1)

                combined_datetime = timezone.make_aware(
                    datetime.datetime.combine(date, shift_schedule.auto_punch_out_time)
                )

                if combined_datetime < timezone.now():
                    try:
                        clock_out(
                            Request(
                                user=attendance.employee_id.employee_user_id,
                                date=date,
                                time=shift_schedule.auto_punch_out_time,
                                datetime=combined_datetime,
                            )
                        )
                    except Exception as e:
                        logger.error(f"auto_punch_out error: {e}")


def _notification_already_sent(user, verb, date):
    from notifications.models import Notification

    return Notification.objects.filter(
        recipient=user,
        verb=verb,
        timestamp__date=date,
    ).exists()


def send_check_in_reminders():
    """Send one check-in reminder per scheduled employee for the day."""
    from attendance.models import Attendance
    from employee.models import Employee
    from notifications.signals import notify

    today = timezone.localdate()

    # Monday=0 ... Friday=4, Saturday=5, Sunday=6
    if today.weekday() >= 5:
        return

    employees = Employee.objects.filter(is_active=True)

    for employee in employees:
        try:
            user = employee.employee_user_id
            if not user:
                continue

            # No reminder when the employee is on approved leave.
            if hasattr(employee, "leaverequest_set") and employee.leaverequest_set.filter(
                status="approved",
                start_date__lte=today,
                end_date__gte=today,
            ).exists():
                continue

            # No reminder on a holiday or an unscheduled day.
            if employee.get_shift_schedule() is None:
                continue

            # Already checked in today.
            if Attendance.objects.filter(
                employee_id=employee,
                attendance_date=today,
                attendance_clock_in__isnull=False,
            ).exists():
                continue

            verb = f"Check-In Reminder: Please check in for {today}"

            if _notification_already_sent(user, verb, today):
                continue

            notify.send(
                employee,
                recipient=user,
                verb=verb,
                verb_en=verb,
                redirect="/attendance/view-my-attendance/",
                icon="time-outline",
            )
        except Exception as e:
            logger.error(f"Check-in reminder error for {employee}: {e}")


def send_missing_checkout_reminders():
    """Notify employees who checked in today but have not checked out."""
    from attendance.models import Attendance
    from employee.models import Employee
    from notifications.signals import notify

    today = timezone.localdate()

    if today.weekday() >= 5:
        return

    attendances = Attendance.objects.filter(
        attendance_date=today,
        attendance_clock_in__isnull=False,
        attendance_clock_out__isnull=True,
        employee_id__is_active=True,
    ).select_related("employee_id", "employee_id__employee_user_id")

    for attendance in attendances:
        try:
            employee = attendance.employee_id
            user = employee.employee_user_id

            if not user:
                continue

            verb = f"Missing Check-Out: Please check out for {today}"

            if _notification_already_sent(user, verb, today):
                continue

            notify.send(
                employee,
                recipient=user,
                verb=verb,
                verb_en=verb,
                redirect="/attendance/view-my-attendance/",
                icon="alert-circle-outline",
            )
        except Exception as e:
            logger.error(
                f"Missing checkout reminder error for attendance "
                f"{attendance.id}: {e}"
            )


def create_work_record():
    from attendance.models import WorkRecords
    from employee.models import Employee

    date = datetime.date.today()
    work_records = WorkRecords.objects.filter(date=date).values_list(
        "employee_id", flat=True
    )
    employees = Employee.objects.exclude(id__in=work_records)
    records_to_create = []

    for employee in employees:
        try:
            shift_schedule = employee.get_shift_schedule()
            if shift_schedule is None:
                continue

            shift = employee.get_shift()
            record = WorkRecords(
                employee_id=employee,
                date=date,
                work_record_type="DFT",
                shift_id=shift,
                message="",
            )
            records_to_create.append(record)
        except Exception as e:
            logger.error(f"Error preparing work record for {employee}: {e}")

    if records_to_create:
        try:
            WorkRecords.objects.bulk_create(records_to_create, ignore_conflicts=True)
        except Exception as e:
            logger.error(f"Failed to bulk create work records: {e}")
