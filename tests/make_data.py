"""Generate a large, awkward dataset (about 100,000 attendance records) for speed, explain and cross-check tests.

Used by tests/test_explain.py. Can also be run by hand to load a database for manual load testing:

    MONGO_DB=attendance_load python tests/make_data.py --yes [N_LOGS]

WARNING: it EMPTIES the `employees` and `attendance_logs` collections of the database named in MONGO_DB.

What it contains on purpose: weekend records, overnight-shift employees, an 08:00-17:00 shift, employees who join
mid-year, LEAVE / ABSENT records, half days, open (not punched out) records, legacy records without half_day/history,
and employees with no logs at all.
"""
import os
import random
import sys
from datetime import date, datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.main import attendance_date, derive  # noqa: E402

IST = timezone(timedelta(hours=5, minutes=30))
DEPARTMENTS = ["Engineering", "Sales", "Support", "Marketing", "Finance", "Operations"]


def _at(day: date, hhmm: str, minutes: int = 0) -> datetime:
    h, m = (int(x) for x in hhmm.split(":"))
    return datetime(day.year, day.month, day.day, h, m, tzinfo=IST) + timedelta(minutes=minutes)


def populate(db, n_logs: int = 100_000, n_employees: int = 450, seed: int = 11) -> dict:
    rng = random.Random(seed)
    db.employees.delete_many({})
    db.attendance_logs.delete_many({})

    employees = []
    for i in range(1, n_employees + 1):
        roll = rng.random()
        shift = ("22:00", "06:00") if roll < 0.05 else ("08:00", "17:00") if roll < 0.10 else ("09:30", "18:30")
        if i % 25 == 0:
            joined = date(2026, 1, 1) + timedelta(days=rng.randint(0, 330))      # joins mid-year (often mid-month)
        else:
            joined = date(2025, 6, 1) + timedelta(days=rng.randint(0, 200))
        employees.append({
            "emp_code": f"EMP{i:04d}", "name": f"Person {i}", "email": f"p{i}@example.com",
            "department": rng.choice(DEPARTMENTS), "shift_start": shift[0], "shift_end": shift[1],
            "joined_on": joined.isoformat(),
            "created_at": datetime(joined.year, joined.month, joined.day, 4, 30, tzinfo=timezone.utc),
        })
    # the last three employees never log anything
    no_logs = {e["emp_code"] for e in employees[-3:]}
    db.employees.insert_many([dict(e) for e in employees])

    logs, day = [], date(2025, 9, 1)
    while len(logs) < n_logs and day <= date(2026, 12, 31):
        weekend = day.weekday() >= 5
        for emp in employees:
            if len(logs) >= n_logs:
                break
            if emp["emp_code"] in no_logs or emp["joined_on"] > day.isoformat():
                continue
            if rng.random() > (0.03 if weekend else 0.96):
                continue
            roll = rng.random()
            status = ("PRESENT" if roll < 0.88 else "WFH" if roll < 0.93 else "ON_DUTY" if roll < 0.95
                      else "LEAVE" if roll < 0.98 else "ABSENT")
            doc = {"emp_code": emp["emp_code"], "date": day.isoformat(), "status": status}
            if status in ("LEAVE", "ABSENT"):
                doc.update(punch_in=None, punch_out=None, work_hours=None, late_minutes=0, overtime_minutes=0,
                           half_day=False, history=[])
            else:
                offset = rng.choice([-10, -5, 0, 0, 0, 5, 9, 11, 25, 75]) + rng.randint(0, 2)
                punch_in = _at(day, emp["shift_start"], offset)
                if rng.random() < 0.01:
                    punch_out = None                                              # open record
                else:
                    minutes = rng.choice([200, 250, 480, 520, 540, 545, 560, 600, 640])
                    punch_out = punch_in + timedelta(minutes=minutes, seconds=rng.randint(0, 59))
                punch_in = punch_in.astimezone(timezone.utc)
                doc_date = attendance_date(punch_in, emp["shift_start"], emp["shift_end"])
                doc.update(date=doc_date, punch_in=punch_in,
                           punch_out=punch_out.astimezone(timezone.utc) if punch_out else None)
                doc.update(derive(emp, doc_date, status, punch_in, doc["punch_out"]))
                doc["history"] = []
                if rng.random() < 0.05:
                    for key in ("half_day", "history"):                            # legacy records omit these
                        doc.pop(key)
            logs.append(doc)
        day += timedelta(days=1)

    # (emp_code, date) must stay unique; a punch-in near midnight on an overnight shift could collide
    seen, unique = set(), []
    for doc in logs:
        key = (doc["emp_code"], doc["date"])
        if key not in seen:
            seen.add(key)
            unique.append(doc)
    for start in range(0, len(unique), 10_000):
        db.attendance_logs.insert_many(unique[start:start + 10_000], ordered=False)
    return {"employees": len(employees), "logs": len(unique)}


if __name__ == "__main__":
    from app.main import db  # reads MONGO_URI / MONGO_DB from the environment or .env

    args = [a for a in sys.argv[1:] if a != "--yes"]
    if "--yes" not in sys.argv:
        sys.exit(f"This empties '{db.name}'. Re-run with --yes to confirm.")
    print(populate(db, int(args[0]) if args else 100_000))
