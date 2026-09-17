import json
import os
import re
import urllib.request
from datetime import timedelta

from django.utils import timezone
from rest_framework.authentication import SessionAuthentication
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from attendance.models import Attendance
from attendance.views.dashboard import find_late_come
from employee.models import Employee


OLLAMA_URL = os.getenv("OLLAMA_URL", "http://172.16.16.111:11434")
OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "qwen2.5:3b")


def _today():
    return timezone.localdate()


def _employee_queryset():
    return Employee.objects.filter(is_active=True).select_related(
        "employee_work_info__department_id"
    )


def _extract_department(question):
    patterns = [
        r"\bin\s+(.+?)(?:\?|$)",
        r"\bfrom\s+(.+?)(?:\?|$)",
        r"\bdepartment(?:\s+is|\s*:)?\s+(.+?)(?:\?|$)",
    ]
    for pattern in patterns:
        match = re.search(pattern, question, re.IGNORECASE)
        if match:
            value = match.group(1).strip(" .?!")
            if value:
                return value
    return None


def _hr_query(question):
    q = question.lower().strip()
    today = _today()

    # Total active employees
    if (
        ("how many" in q and "employee" in q)
        or "total employees" in q
        or "employee count" in q
        or "headcount" in q
    ) and "today" not in q:
        count = _employee_queryset().count()
        return {
            "intent": "employee_count",
            "facts": {"active_employee_count": count},
        }

    # Employees in a department
    if (
        ("employee" in q and ("department" in q or " in " in q))
        and ("how many" in q or "who" in q or "list" in q or "show" in q)
    ):
        department = _extract_department(question)
        if department:
            employees = list(
                _employee_queryset()
                .filter(
                    employee_work_info__department_id__department__icontains=department
                )
                .values(
                    "id",
                    "employee_first_name",
                    "employee_last_name",
                )
            )
            return {
                "intent": "department_employees",
                "facts": {
                    "department_requested": department,
                    "employee_count": len(employees),
                    "employees": [
                        {
                            "id": e["id"],
                            "name": f'{e["employee_first_name"]} {e["employee_last_name"]}'.strip(),
                        }
                        for e in employees
                    ],
                },
            }

    # Present today
    if "present" in q and ("today" in q or "now" in q):
        count = (
            Attendance.objects.filter(
                attendance_date=today,
                employee_id__is_active=True,
            )
            .values("employee_id")
            .distinct()
            .count()
        )
        return {
            "intent": "present_today",
            "facts": {"date": str(today), "present_employee_count": count},
        }

    # WFH today
    if (
        ("wfh" in q or "work from home" in q or "working from home" in q)
        and ("today" in q or "now" in q)
    ):
        rows = (
            Attendance.objects.filter(
                attendance_date=today,
                employee_id__is_active=True,
                work_location="wfh",
            )
            .values(
                "employee_id",
                "employee_id__employee_first_name",
                "employee_id__employee_last_name",
            )
            .distinct()
        )
        employees = [
            {
                "name": f'{r["employee_id__employee_first_name"]} {r["employee_id__employee_last_name"]}'.strip()
            }
            for r in rows
        ]
        return {
            "intent": "wfh_today",
            "facts": {
                "date": str(today),
                "wfh_employee_count": len(employees),
                "employees": employees,
            },
        }

    # Missing checkout today
    if (
        ("missing checkout" in q)
        or ("not checked out" in q)
        or ("did not check out" in q)
        or ("haven't checked out" in q)
    ):
        rows = (
            Attendance.objects.filter(
                attendance_date=today,
                employee_id__is_active=True,
                attendance_clock_in__isnull=False,
                attendance_clock_out__isnull=True,
            )
            .values(
                "employee_id",
                "employee_id__employee_first_name",
                "employee_id__employee_last_name",
            )
            .distinct()
        )
        employees = [
            {
                "name": f'{r["employee_id__employee_first_name"]} {r["employee_id__employee_last_name"]}'.strip()
            }
            for r in rows
        ]
        return {
            "intent": "missing_checkout",
            "facts": {
                "date": str(today),
                "employee_count": len(employees),
                "employees": employees,
            },
        }

    # Late today
    if "late" in q and ("today" in q or "now" in q):
        rows = find_late_come(today, end_date=today)
        employee_ids = rows.values_list("employee_id", flat=True).distinct()
        employees = list(
            Employee.objects.filter(
                id__in=employee_ids,
                is_active=True,
            ).values("employee_first_name", "employee_last_name")
        )
        names = [
            {
                "name": f'{e["employee_first_name"]} {e["employee_last_name"]}'.strip()
            }
            for e in employees
        ]
        return {
            "intent": "late_today",
            "facts": {
                "date": str(today),
                "late_employee_count": len(names),
                "employees": names,
            },
        }

    return None


def _ask_ollama(question, facts):
    prompt = (
        "You are the Horilla HR Assistant. "
        "Answer the HR user's question using ONLY the supplied facts. "
        "Do not invent employees, numbers, dates, policies, or other information. "
        "If a fact is missing, say it is not available. "
        "Keep the answer concise and professional.\n\n"
        f"User question: {question}\n"
        f"Verified HR facts: {json.dumps(facts, ensure_ascii=False)}"
    )

    payload = json.dumps(
        {
            "model": OLLAMA_MODEL,
            "prompt": prompt,
            "stream": False,
            "options": {
                "temperature": 0,
            },
        }
    ).encode()

    request = urllib.request.Request(
        f"{OLLAMA_URL}/api/generate",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )

    with urllib.request.urlopen(request, timeout=120) as response:
        data = json.loads(response.read().decode())

    return data.get("response", "").strip()


class AIHRAssistantAPIView(APIView):
    """
    Read-only AI HR Assistant.

    The AI never receives database access. Django first executes a
    predefined read-only HR query and sends only the resulting facts
    to the local Ollama model.
    """

    authentication_classes = [SessionAuthentication]
    permission_classes = [IsAuthenticated]

    def post(self, request):
        if not request.user.is_superuser:
            return Response(
                {"error": "AI HR Assistant is currently available to HR administrators only."},
                status=403,
            )

        question = str(request.data.get("question", "")).strip()

        if not question:
            return Response(
                {"error": "Please provide a question."},
                status=400,
            )

        if len(question) > 500:
            return Response(
                {"error": "Question is too long. Maximum length is 500 characters."},
                status=400,
            )

        result = _hr_query(question)

        if result is None:
            return Response(
                {
                    "answer": (
                        "I can currently answer questions about employee counts, "
                        "employees by department, present employees, WFH employees, "
                        "late arrivals, and missing checkouts."
                    ),
                    "intent": "unsupported",
                },
                status=200,
            )

        try:
            answer = _ask_ollama(question, result["facts"])
        except Exception:
            answer = "The local AI service is temporarily unavailable."

        return Response(
            {
                "answer": answer,
                "intent": result["intent"],
                "facts": result["facts"],
            },
            status=200,
        )
