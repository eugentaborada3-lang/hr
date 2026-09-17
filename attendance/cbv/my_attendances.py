"""
My attendances
"""

from datetime import date, timedelta
from typing import Any

from django.shortcuts import render
from django.utils import timezone
from django.db.models import Q

from django.urls import reverse
from django.utils.decorators import method_decorator
from django.utils.translation import gettext_lazy as _

from attendance.filters import AttendanceFilters
from attendance.models import Attendance, AttendanceLateComeEarlyOut, WorkRecords
from horilla_views.cbv_methods import login_required
from horilla_views.generic.cbv.views import (
    HorillaDetailedView,
    HorillaListView,
    HorillaNavView,
    TemplateView,
)


@method_decorator(login_required, name="dispatch")
class MyAttendances(TemplateView):
    """
    My attendances
    """

    template_name = "cbv/my_attendances/my_attendances.html"



@method_decorator(login_required, name="dispatch")
class MyAttendanceSummary(TemplateView):
    """Read-only summary for the logged-in employee attendance today."""

    template_name = "cbv/my_attendances/my_attendance_summary.html"

    def get(self, request, *args, **kwargs):
        employee = request.user.employee_get
        today = date.today()

        attendance = (
            Attendance.objects.filter(
                employee_id=employee,
                attendance_date=today,
            )
            .order_by("-id")
            .first()
        )

        late_early = AttendanceLateComeEarlyOut.objects.filter(
            employee_id=employee,
            attendance_id__attendance_date=today,
        ).values_list("type", flat=True)

        work_record = WorkRecords.objects.filter(
            employee_id=employee,
            date=today,
        ).first()

        context = {
            "today": today,
            "attendance": attendance,
            "is_late": "late_come" in late_early,
            "is_early": "early_out" in late_early,
            "work_record": work_record,
        }
        return render(request, self.template_name, context)
@method_decorator(login_required, name="dispatch")
class MyMonthlyAttendanceSummary(TemplateView):
    """
    Read-only monthly attendance summary for the logged-in employee.
    """

    template_name = "cbv/my_attendances/my_monthly_attendance_summary.html"

    def get(self, request, *args, **kwargs):
        from calendar import monthrange
        from datetime import datetime

        from django.db.models import Sum
        from leave.models import LeaveRequest

        employee = request.user.employee_get
        today = timezone.localdate()

        month_value = request.GET.get("month", today.strftime("%Y-%m"))
        try:
            selected_month = datetime.strptime(month_value, "%Y-%m").date()
        except ValueError:
            selected_month = today.replace(day=1)

        from_date = selected_month.replace(day=1)
        to_date = selected_month.replace(
            day=monthrange(selected_month.year, selected_month.month)[1]
        )

        # Do not include future days when viewing the current month.
        if selected_month.year == today.year and selected_month.month == today.month:
            to_date = today

        work_records = WorkRecords.objects.filter(
            employee_id=employee,
            date__range=(from_date, to_date),
        )

        status_counts = {
            "present": work_records.filter(work_record_type="FDP").count(),
            "half_days": work_records.filter(work_record_type="HDP").count(),
            "absent": work_records.filter(work_record_type="ABS").count(),
            "holiday_week_off": work_records.filter(work_record_type="HD").count(),
        }

        attendance_qs = Attendance.objects.filter(
            employee_id=employee,
            attendance_date__range=(from_date, to_date),
        ).values(
            "attendance_date",
            "attendance_clock_in",
            "attendance_clock_out",
            "attendance_clock_out_date",
            "at_work_second",
            "attendance_overtime",
            "attendance_overtime_approve",
        )

        attendance_totals = attendance_qs.aggregate(
            worked_seconds=Sum("at_work_second"),
        )

        # Company attendance rules:
        # - Duty starts at 08:00 AM.
        # - Check-In after 08:00 AM = Late Arrival.
        # - Duty ends at 06:00 PM.
        # - Check-Out after 06:00 PM = Overtime recorded.
        # - HR decides whether the recorded overtime is approved.
        from datetime import time as dt_time

        late_arrivals = 0
        overtime_seconds = 0
        approved_overtime_seconds = 0

        for record in attendance_qs:
            clock_in = record["attendance_clock_in"]
            clock_out = record["attendance_clock_out"]

            if clock_in and clock_in > dt_time(8, 0):
                late_arrivals += 1

            if clock_out:
                out_date = (
                    record["attendance_clock_out_date"]
                    or record["attendance_date"]
                )
                overtime_start = datetime.combine(out_date, dt_time(18, 0))
                checkout_dt = datetime.combine(out_date, clock_out)

                if checkout_dt > overtime_start:
                    record_overtime_seconds = int(
                        (checkout_dt - overtime_start).total_seconds()
                    )
                    overtime_seconds += record_overtime_seconds

                    if record["attendance_overtime_approve"]:
                        approved_overtime_seconds += record_overtime_seconds

        approved_leave_days = set()
        leave_requests = LeaveRequest.objects.filter(
            employee_id=employee,
            status="approved",
        ).filter(
            Q(start_date__lte=to_date),
            Q(end_date__gte=from_date) | Q(end_date__isnull=True),
        )

        for leave in leave_requests:
            leave_start = max(leave.start_date, from_date)
            leave_end = min(leave.end_date or leave.start_date, to_date)
            current = leave_start
            while current <= leave_end:
                approved_leave_days.add(current)
                current += timedelta(days=1)

        early_departures = AttendanceLateComeEarlyOut.objects.filter(
            employee_id=employee,
            attendance_id__attendance_date__range=(from_date, to_date),
            type="early_out",
        ).count()

        worked_seconds = attendance_totals["worked_seconds"] or 0
        worked_label = f"{worked_seconds // 3600}h {(worked_seconds % 3600) // 60:02d}m"
        overtime_label = f"{overtime_seconds // 3600}h {(overtime_seconds % 3600) // 60:02d}m"
        approved_overtime_label = (
            f"{approved_overtime_seconds // 3600}h "
            f"{(approved_overtime_seconds % 3600) // 60:02d}m"
        )

        context = {
            "from_date": from_date,
            "to_date": to_date,
            "month_value": from_date.strftime("%Y-%m"),
            "month_label": from_date.strftime("%B %Y"),
            "total_present": status_counts["present"],
            "half_days": status_counts["half_days"],
            "absent": status_counts["absent"],
            "approved_leave": len(approved_leave_days),
            "holiday_week_off": status_counts["holiday_week_off"],
            "worked_seconds": worked_seconds,
            "overtime_seconds": overtime_seconds,
            "approved_overtime_seconds": approved_overtime_seconds,
            "worked_label": worked_label,
            "overtime_label": overtime_label,
            "approved_overtime_label": approved_overtime_label,
            "late_arrivals": late_arrivals,
            "early_departures": early_departures,
        }

        return render(request, self.template_name, context)


class MyAttendancesListView(HorillaListView):

    model = Attendance
    filter_class = AttendanceFilters
    columns = [
        (_("Employee"), "employee_id", "employee_id__get_avatar"),
        (_("Date"), "attendance_date"),
        (_("Day"), "attendance_day"),
        (_("Check-In"), "attendance_clock_in"),
        (_("In Date"), "attendance_clock_in_date"),
        (_("Check-Out"), "attendance_clock_out"),
        (_("Out Date"), "attendance_clock_out_date"),
        (_("Shift"), "shift_id"),
        (_("Work Type"), "work_type_id"),
        (_("Min Hour"), "minimum_hour"),
        (_("At Work"), "attendance_worked_hour"),
        (_("Pending Hour"), "hours_pending"),
        (_("Overtime"), "attendance_overtime"),
    ]
    default_columns = [
        (_("Employee"), "employee_id", "employee_id__get_avatar"),
        (_("Date"), "attendance_date"),
        (_("Check-In"), "attendance_clock_in"),
        (_("Check-Out"), "attendance_clock_out"),
        (_("Shift"), "shift_id"),
        (_("At Work"), "attendance_worked_hour"),
    ]

    row_attrs = """
                hx-get='{my_attendance_detail}?instance_ids={ordered_ids}'
                hx-target="#genericModalBody"
                data-target="#genericModal"
                data-toggle="oh-modal-toggle"
                """

    sortby_mapping = [
        (_("Employee"), "employee_id__get_full_name", "employee_id__get_avatar"),
        (_("Date"), "attendance_date"),
        (_("Day"), "attendance_day__day"),
        (_("Check-In"), "attendance_clock_in"),
        (_("Shift"), "shift_id__employee_shift"),
        (_("Work Type"), "work_type_id__work_type"),
        (_("Min Hour"), "minimum_hour"),
        (_("Pending Hour"), "hours_pending"),
        (_("In Date"), "attendance_clock_in_date"),
        (_("Check-Out"), "attendance_clock_out"),
        (_("Out Date"), "attendance_clock_out_date"),
        (_("At Work"), "attendance_worked_hour"),
        (_("Overtime"), "attendance_overtime"),
    ]


@method_decorator(login_required, name="dispatch")
class MyAttendanceList(MyAttendancesListView):
    """
    List view
    """

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.search_url = reverse("my-attendance-list")

    row_status_indications = [
        (
            "approved-request--dot",
            _("Approved Request"),
            """
            onclick="
                $('#applyFilter').closest('form').find('[name=is_validate_request_approved]').val('true');
                $('[name=attendance_validated]').val('unknown').change();
                $('[name=is_validate_request]').val('unknown').change();
                $('#applyFilter').click();

            "
            """,
        ),
        (
            "requested--dot",
            _("Requested"),
            """
            onclick="
                $('#applyFilter').closest('form').find('[name=is_validate_request]').val('true');
                $('[name=attendance_validated]').val('unknown').change();
                $('[name=is_validate_request_approved]').val('unknown').change();
                $('#applyFilter').click();

            "
            """,
        ),
        (
            "not-validated--dot",
            _("Not Validated"),
            """
            onclick="
                $('#applyFilter').closest('form').find('[name=attendance_validated]').val('false');
                $('[name=is_validate_request]').val('unknown').change();
                $('[name=is_validate_request_approved]').val('unknown').change();
                $('#applyFilter').click();
            "
            """,
        ),
        (
            "validated--dot",
            _("Validated"),
            """
            onclick="
                $('#applyFilter').closest('form').find('[name=attendance_validated]').val('true');
                $('[name=is_validate_request]').val('unknown').change();
                $('[name=is_validate_request_approved]').val('unknown').change();
                $('#applyFilter').click();

            "
            """,
        ),
    ]

    row_status_class = "validated-{attendance_validated}  requested-{is_validate_request} approved-request-{is_validate_request_approved}"

    def get_queryset(self):
        queryset = super().get_queryset()
        employee = self.request.user.employee_get
        queryset = queryset.filter(employee_id=employee)
        return queryset


@method_decorator(login_required, name="dispatch")
class MyAttendancestNav(HorillaNavView):
    """
    Nav bar
    """

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.search_url = reverse("my-attendance-list")
        self.search_in = [
            ("shift_id__employee_shift", "Shift"),
            ("work_type_id__work_type", "Work Type"),
        ]

    nav_title = _("My Attendances")
    filter_body_template = "cbv/my_attendances/my_attendance_filter.html"
    filter_instance = AttendanceFilters()
    filter_form_context_name = "form"
    search_swap_target = "#listContainer"
    search_input_attrs = """ hidden """


@method_decorator(login_required, name="dispatch")
class MyAttendancesDetailView(HorillaDetailedView):
    """
    Detail View
    """

    model = Attendance

    title = _("Details")

    header = {
        "title": "employee_id__get_full_name",
        "subtitle": "my_attendance_subtitle",
        "avatar": "employee_id__get_avatar",
    }

    body = [
        (_("Date"), "attendance_date"),
        (_("Day"), "attendance_day"),
        (_("Check-In"), "attendance_clock_in"),
        (_("Check-in Date"), "attendance_clock_in_date"),
        (_("Check-Out"), "attendance_clock_out"),
        (_("Check-out Date"), "attendance_clock_out_date"),
        (_("Shift"), "shift_id"),
        (_("Work Type"), "work_type_id"),
        (_("Min Hour"), "minimum_hour"),
        (_("At Work"), "attendance_worked_hour"),
        (_("Pending Hour"), "hours_pending"),
        (_("Overtime"), "attendance_overtime"),
        (_("Activities"), "attendance_detail_activity_col", True),
    ]

    actions = [
        {
            "action": _("Request Attendance Correction"),
            "icon": "create-outline",
            "attrs": """
                class="oh-btn oh-btn--secondary"
                data-toggle="oh-modal-toggle"
                data-target="#genericModal"
                hx-get="/attendance/request-attendance/{get_instance_id}"
                hx-target="#genericModalBody"
                style="cursor:pointer;"
            """,
        }
    ]
