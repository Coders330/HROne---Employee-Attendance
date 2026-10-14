"""Small helpers shared by the tests."""
from datetime import datetime, timedelta, timezone

IST = timezone(timedelta(hours=5, minutes=30))
UTC = timezone.utc
EPOCH = datetime(1970, 1, 1, tzinfo=UTC)


def ist(day: str, hhmm: str, seconds: int = 0, micro: int = 0, plus_days: int = 0) -> datetime:
    """An aware datetime for an IST wall-clock time, e.g. ist('2026-07-06', '09:28')."""
    y, m, d = (int(x) for x in day.split("-"))
    h, mi = (int(x) for x in hhmm.split(":"))
    return datetime(y, m, d, h, mi, seconds, micro, tzinfo=IST) + timedelta(days=plus_days)


def ms(dt: datetime) -> int:
    """Epoch milliseconds of an aware datetime."""
    return (dt - EPOCH) // timedelta(milliseconds=1)


def emp_payload(code="EMP0001", **overrides) -> dict:
    body = {
        "emp_code": code,
        "name": "Test Person",
        "email": "test@example.com",
        "department": "Engineering",
        "shift_start": "09:30",
        "shift_end": "18:30",
        "joined_on": "2026-01-05",
    }
    body.update(overrides)
    return body


def emp_doc(code, name, department, joined_on, shift_start="09:30", shift_end="18:30") -> dict:
    """A document as the grader would seed it into the `employees` collection."""
    return {
        "emp_code": code, "name": name, "email": f"{name.lower()}@example.com", "department": department,
        "shift_start": shift_start, "shift_end": shift_end, "joined_on": joined_on,
        "created_at": ist(joined_on, "10:00"),
    }


def log_doc(emp_code, day, status="PRESENT", punch_in=None, punch_out=None, work_hours=None,
            late=0, overtime=0, half_day=False, history=None, legacy=False) -> dict:
    """A document as the grader would seed it into `attendance_logs` (stored values are authoritative)."""
    doc = {
        "emp_code": emp_code, "date": day, "status": status,
        "punch_in": punch_in, "punch_out": punch_out, "work_hours": work_hours,
        "late_minutes": late, "overtime_minutes": overtime,
    }
    if not legacy:  # legacy records omit half_day and history entirely
        doc["half_day"] = half_day
        doc["history"] = history if history is not None else []
    return doc
