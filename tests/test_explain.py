"""Index proof, speed and an independent cross-check on about 100,000 attendance records (needs MongoDB).

Loading the data takes a few seconds. The explain tests assert IXSCAN is in the plan and COLLSCAN is not.
"""
import json
import time
from collections import defaultdict
from datetime import date

import pytest

from app.main import PRESENCE, count_weekdays, div_half_up, month_bounds
from make_data import populate

MONTH = "2026-03"


@pytest.fixture(scope="module")
def big(api, database):
    stats = populate(database, n_logs=100_000)
    assert stats["logs"] > 90_000, stats
    yield api
    database.attendance_logs.delete_many({})      # free the space again (matters on the 512 MB free tier)
    database.employees.delete_many({})


EXPLAIN_CASES = [
    ("attendance_list", {}),
    ("attendance_list", {"emp_code": "EMP0001", "date_from": "2026-03-01", "date_to": "2026-03-31"}),
    ("attendance_list", {"status": "LEAVE", "page": 2, "page_size": 50}),
    ("attendance_list", {"date_from": "2026-03-01", "date_to": "2026-03-31"}),
    ("employee_monthly", {"emp_code": "EMP0001", "month": MONTH}),
    ("department_summary", {"month": MONTH}),
    ("department_summary", {"month": MONTH, "department": "Engineering"}),
    ("late_leaderboard", {"month": MONTH, "limit": 10}),
    ("late_leaderboard", {"month": MONTH, "limit": 10, "department": "Sales"}),
    ("department_trend", {"department": "Engineering", "from": "2026-03-01", "to": "2026-03-31"}),
]


@pytest.mark.parametrize("endpoint,params", EXPLAIN_CASES, ids=[f"{e}-{i}" for i, (e, _) in enumerate(EXPLAIN_CASES)])
def test_explain_uses_an_index_and_never_scans_the_collection(big, endpoint, params):
    r = big.get(f"/admin/explain/{endpoint}", params=params)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["endpoint"] == endpoint and body["collection"] in ("attendance_logs", "employees")
    plan = json.dumps(body["explain"])
    assert "IXSCAN" in plan, plan[:3000]
    assert "COLLSCAN" not in plan, plan[:3000]


def test_explain_input_errors(big):
    assert big.get("/admin/explain/employee_monthly", params={"month": MONTH}).status_code == 422       # emp_code missing
    assert big.get("/admin/explain/department_summary").status_code == 422                             # month missing
    assert big.get("/admin/explain/department_trend", params={"department": "Sales", "from": "2026-03-01"}).status_code == 422
    assert big.get("/admin/explain/not_an_endpoint").status_code == 422
    assert big.get("/admin/explain/department_trend",
                   params={"department": "Nope", "from": "2026-03-01", "to": "2026-03-05"}).status_code == 404


SPEED_CASES = [
    ("/attendance", {}),
    ("/attendance", {"page": 50, "page_size": 100}),
    ("/attendance", {"emp_code": "EMP0010", "date_from": "2026-03-01", "date_to": "2026-03-31"}),
    ("/analytics/employees/EMP0010/monthly", {"month": MONTH}),
    ("/analytics/departments/summary", {"month": MONTH}),
    ("/analytics/leaderboard/late", {"month": MONTH}),
    ("/analytics/departments/Engineering/trend", {"from": "2026-01-01", "to": "2026-03-31"}),
]


@pytest.mark.parametrize("path,params", SPEED_CASES)
def test_endpoints_are_fast_on_100k_records(big, path, params):
    started = time.perf_counter()
    r = big.get(path, params=params)
    elapsed = time.perf_counter() - started
    assert r.status_code == 200, r.text
    assert elapsed < 3.0, f"{path} took {elapsed:.2f}s"


# --------------------------------------------------------------------------- #
# independent cross-checks: compute the same answers in plain Python from the stored values
# --------------------------------------------------------------------------- #
def _month_logs(database, month):
    first, last = month_bounds(month)
    return list(database.attendance_logs.find({"date": {"$gte": first, "$lte": last}})), first, last


def test_leaderboard_matches_a_python_reference(big, database):
    logs, _, _ = _month_logs(database, MONTH)
    employees = {e["emp_code"]: e for e in database.employees.find()}
    totals = defaultdict(lambda: [0, 0])
    for log in logs:
        late = log.get("late_minutes") or 0
        totals[log["emp_code"]][0] += late
        totals[log["emp_code"]][1] += 1 if late > 0 else 0
    ordered = sorted(((t, c, code) for code, (t, c) in totals.items() if t > 0 and code in employees),
                     key=lambda x: (-x[0], x[2]))
    expected, rank = [], 0
    for i, (total, count, code) in enumerate(ordered):
        if i == 0 or total != ordered[i - 1][0]:
            rank = i + 1                                         # standard competition ranking
        expected.append({"rank": rank, "emp_code": code, "name": employees[code]["name"],
                         "department": employees[code]["department"], "total_late_minutes": total,
                         "late_count": count})
    for limit in (1, 5, 50):
        got = big.get("/analytics/leaderboard/late", params={"month": MONTH, "limit": limit}).json()["items"]
        assert got == [row for row in expected if row["rank"] <= limit], limit


def test_department_summary_matches_a_python_reference(big, database):
    logs, first, last = _month_logs(database, MONTH)
    dept_of = {e["emp_code"]: e["department"] for e in database.employees.find({"joined_on": {"$lte": last}})}
    stats = {}
    for dept in dept_of.values():
        stats.setdefault(dept, defaultdict(int))
    for dept in dept_of.values():
        stats[dept]["headcount"] += 1
    for log in logs:
        dept = dept_of.get(log["emp_code"])
        if dept is None:
            continue
        s = stats[dept]
        present = log["status"] in PRESENCE
        if present and date.fromisoformat(log["date"]).weekday() < 5:
            s["halves"] += 1 if log.get("half_day") else 2
        if present and log.get("work_hours") is not None:
            s["cents"] += round(log["work_hours"] * 100)
            s["count"] += 1
        late = log.get("late_minutes") or 0
        s["late_count"] += 1 if late > 0 else 0
        s["late_total"] += late
        s["leave"] += 1 if log["status"] == "LEAVE" else 0
        s["on_duty"] += 1 if log["status"] == "ON_DUTY" else 0
    expected = [{
        "department": dept, "headcount": s["headcount"], "present_days": s["halves"] / 2,
        "avg_work_hours": None if s["count"] == 0 else div_half_up(s["cents"], s["count"]) / 100,
        "late_count": s["late_count"], "total_late_minutes": s["late_total"], "leave_count": s["leave"],
        "on_duty_count": s["on_duty"],
    } for dept, s in sorted(stats.items())]
    got = big.get("/analytics/departments/summary", params={"month": MONTH}).json()["items"]
    assert got == expected


def test_employee_monthly_matches_a_python_reference(big, database):
    _, first, last = _month_logs(database, MONTH)
    sample = [e for e in database.employees.find().sort("emp_code", 1)][::40]
    assert sample
    for emp in sample:
        halves = leave = late_count = late_total = overtime = 0
        for log in database.attendance_logs.find({"emp_code": emp["emp_code"], "date": {"$gte": first, "$lte": last}}):
            if log["status"] in PRESENCE and date.fromisoformat(log["date"]).weekday() < 5:
                halves += 1 if log.get("half_day") else 2
            leave += 1 if log["status"] == "LEAVE" else 0
            late = log.get("late_minutes") or 0
            late_count += 1 if late > 0 else 0
            late_total += late
            overtime += log.get("overtime_minutes") or 0
        working = count_weekdays(max(date.fromisoformat(emp["joined_on"]), date.fromisoformat(first)),
                                 date.fromisoformat(last))
        got = big.get(f"/analytics/employees/{emp['emp_code']}/monthly", params={"month": MONTH}).json()
        assert got == {
            "emp_code": emp["emp_code"], "month": MONTH, "working_days": working, "present_days": halves / 2,
            "leave_days": leave, "late_count": late_count, "total_late_minutes": late_total,
            "total_overtime_minutes": overtime,
            "attendance_pct": None if working == 0 else div_half_up(halves * 5000, working) / 100,
        }, emp["emp_code"]


def test_trend_returns_one_row_per_day_with_consistent_numbers(big):
    r = big.get("/analytics/departments/Sales/trend", params={"from": "2026-03-01", "to": "2026-05-31"})
    assert r.status_code == 200
    rows = r.json()["items"]
    assert len(rows) == 92 and len({row["date"] for row in rows}) == 92
    for row in rows:
        assert (row["attendance_rate"] is None) == (not row["is_working_day"] or row["headcount"] == 0)
        if row["attendance_rate"] is not None:
            assert 0 <= row["attendance_rate"] <= 1.5      # can exceed 1 only through weekend-free anomalies; sanity bound
        assert row["moving_avg_7d"] is None or row["moving_avg_7d"] >= 0
