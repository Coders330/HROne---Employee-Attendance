"""Analytics tests on a small dataset whose answers were worked out by hand (needs MongoDB).

July 2026: Wed Jul 1 ... Mon Jul 6, Tue 7, Wed 8, Thu 9, Fri 10, Sat 11, Sun 12, Mon 13 ... 23 weekdays in the month.

Awkward cases on purpose: an employee with no logs at all (Lata), people who join mid-month (Vikram, Sam) or after
the month (Zoe), a weekend record (Rohit, Sat Jul 11), an overnight shift (Meera), an open record (Karan, Vikram),
a legacy record without half_day/history (Vikram, Jul 14), a tie at the leaderboard cutoff, and a log whose
employee does not exist (EMP9999).
"""
from datetime import date, timedelta

import pytest

from helpers import emp_doc, ist, log_doc

EMPLOYEES = [
    emp_doc("EMP0001", "Asha", "Engineering", "2026-01-05"),
    emp_doc("EMP0002", "Vikram", "Engineering", "2026-07-13"),
    emp_doc("EMP0003", "Neha", "Sales", "2026-02-01"),
    emp_doc("EMP0004", "Rohit", "Sales", "2026-02-01"),
    emp_doc("EMP0005", "Meera", "Support", "2026-03-10", shift_start="22:00", shift_end="06:00"),
    emp_doc("EMP0006", "Karan", "Support", "2026-03-10"),
    emp_doc("EMP0007", "Sam", "Sales", "2026-07-20"),        # joins late in July, no logs
    emp_doc("EMP0008", "Zoe", "Sales", "2026-08-03"),        # joins after July
    emp_doc("EMP0009", "Lata", "Marketing", "2026-01-01"),   # no logs at all
]

LOGS = [
    # Asha
    log_doc("EMP0001", "2026-07-06", "PRESENT", ist("2026-07-06", "09:28"), ist("2026-07-06", "18:35"), 9.12),
    log_doc("EMP0001", "2026-07-07", "PRESENT", ist("2026-07-07", "09:28"), ist("2026-07-07", "18:30"), 9.03),
    log_doc("EMP0001", "2026-07-10", "ON_DUTY", ist("2026-07-10", "09:50"), ist("2026-07-10", "18:30"), 8.67, late=20),
    # Vikram (joined Jul 13): a legacy record and an open record
    log_doc("EMP0002", "2026-07-14", "PRESENT", ist("2026-07-14", "09:32"), ist("2026-07-14", "18:31"), 8.98, legacy=True),
    log_doc("EMP0002", "2026-07-15", "PRESENT", ist("2026-07-15", "10:30"), None, None, late=60),
    # Neha
    log_doc("EMP0003", "2026-07-06", "PRESENT", ist("2026-07-06", "10:05"), ist("2026-07-06", "19:10"), 9.08,
            late=35, overtime=40),
    log_doc("EMP0003", "2026-07-08", "LEAVE"),
    log_doc("EMP0003", "2026-07-09", "PRESENT", ist("2026-07-09", "09:30"), ist("2026-07-09", "18:30"), 9.0),
    # Rohit: a half day, an absence and a Saturday record
    log_doc("EMP0004", "2026-07-07", "WFH", ist("2026-07-07", "09:30"), ist("2026-07-07", "13:00"), 3.5, half_day=True),
    log_doc("EMP0004", "2026-07-08", "ABSENT"),
    log_doc("EMP0004", "2026-07-11", "PRESENT", ist("2026-07-11", "09:50"), ist("2026-07-11", "18:30"), 8.67, late=20),
    # Meera: overnight shift, the record belongs to the day the shift started
    log_doc("EMP0005", "2026-07-06", "PRESENT", ist("2026-07-06", "21:55"), ist("2026-07-06", "06:40", plus_days=1),
            8.75, overtime=40),
    # Karan: open record
    log_doc("EMP0006", "2026-07-07", "PRESENT", ist("2026-07-07", "09:35"), None, None),
    # a log whose employee does not exist: must be ignored everywhere
    log_doc("EMP9999", "2026-07-06", "PRESENT", ist("2026-07-06", "11:00"), ist("2026-07-06", "19:00"), 8.0, late=99),
]


@pytest.fixture()
def data(client, seed):
    seed(employees=EMPLOYEES, logs=LOGS)
    return client


def _monthly(client, code, month="2026-07"):
    r = client.get(f"/analytics/employees/{code}/monthly", params={"month": month})
    assert r.status_code == 200, r.text
    return r.json()


# --------------------------------------------------------------------------- #
# employee monthly
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("code,expected", [
    ("EMP0003", dict(working_days=23, present_days=2.0, leave_days=1, late_count=1, total_late_minutes=35,
                     total_overtime_minutes=40, attendance_pct=8.7)),
    # half day = 0.5; the Saturday record does not count as a present day but its 20 late minutes do count
    ("EMP0004", dict(working_days=23, present_days=0.5, leave_days=0, late_count=1, total_late_minutes=20,
                     total_overtime_minutes=0, attendance_pct=2.17)),
    ("EMP0001", dict(working_days=23, present_days=3.0, leave_days=0, late_count=1, total_late_minutes=20,
                     total_overtime_minutes=0, attendance_pct=13.04)),
    # joined Jul 13: 15 working days, and the open record still counts as present
    ("EMP0002", dict(working_days=15, present_days=2.0, leave_days=0, late_count=1, total_late_minutes=60,
                     total_overtime_minutes=0, attendance_pct=13.33)),
    ("EMP0005", dict(working_days=23, present_days=1.0, leave_days=0, late_count=0, total_late_minutes=0,
                     total_overtime_minutes=40, attendance_pct=4.35)),
    ("EMP0006", dict(working_days=23, present_days=1.0, leave_days=0, late_count=0, total_late_minutes=0,
                     total_overtime_minutes=0, attendance_pct=4.35)),
    # joined Jul 20: Jul 20-24 and Jul 27-31 = 10 working days, no logs -> zeros, not an error
    ("EMP0007", dict(working_days=10, present_days=0.0, leave_days=0, late_count=0, total_late_minutes=0,
                     total_overtime_minutes=0, attendance_pct=0.0)),
    # joined after the month: no working days, so the percentage is null
    ("EMP0008", dict(working_days=0, present_days=0.0, leave_days=0, late_count=0, total_late_minutes=0,
                     total_overtime_minutes=0, attendance_pct=None)),
    ("EMP0009", dict(working_days=23, present_days=0.0, leave_days=0, late_count=0, total_late_minutes=0,
                     total_overtime_minutes=0, attendance_pct=0.0)),
])
def test_employee_monthly(data, code, expected):
    assert _monthly(data, code) == {"emp_code": code, "month": "2026-07", **expected}


def test_employee_monthly_month_without_logs_and_errors(data):
    aug = _monthly(data, "EMP0003", "2026-08")      # August 2026 has 21 weekdays
    assert aug["working_days"] == 21 and aug["present_days"] == 0.0 and aug["attendance_pct"] == 0.0
    assert data.get("/analytics/employees/EMP9999/monthly", params={"month": "2026-07"}).status_code == 404
    for month in ("2026-13", "2026-7", "202607", "July"):
        assert data.get("/analytics/employees/EMP0003/monthly", params={"month": month}).status_code == 422
    assert data.get("/analytics/employees/EMP0003/monthly").status_code == 422


# --------------------------------------------------------------------------- #
# department summary
# --------------------------------------------------------------------------- #
def _summary(client, **params):
    r = client.get("/analytics/departments/summary", params=params)
    assert r.status_code == 200, r.text
    return r.json()


def test_department_summary_july(data):
    result = _summary(data, month="2026-07")
    assert result["month"] == "2026-07"
    assert result["items"] == [                     # sorted by department; EMP9999 is ignored
        dict(department="Engineering", headcount=2, present_days=5.0, avg_work_hours=8.95, late_count=2,
             total_late_minutes=80, leave_count=0, on_duty_count=1),
        # Lata has no logs and still counts
        dict(department="Marketing", headcount=1, present_days=0.0, avg_work_hours=None, late_count=0,
             total_late_minutes=0, leave_count=0, on_duty_count=0),
        # Sam (joined Jul 20, no logs) counts, Zoe (joins in August) does not; average is over records:
        # (9.08 + 9.0 + 3.5 + 8.67) / 4 = 7.5625 -> 7.56, not an average of per-person averages
        dict(department="Sales", headcount=3, present_days=2.5, avg_work_hours=7.56, late_count=2,
             total_late_minutes=55, leave_count=1, on_duty_count=0),
        # Karan's open record has no work_hours, so only Meera's 8.75 is in the average
        dict(department="Support", headcount=2, present_days=2.0, avg_work_hours=8.75, late_count=0,
             total_late_minutes=0, leave_count=0, on_duty_count=0),
    ]


def test_department_summary_headcount_follows_the_month(data):
    june = {i["department"]: i["headcount"] for i in _summary(data, month="2026-06")["items"]}
    assert june == {"Engineering": 1, "Marketing": 1, "Sales": 2, "Support": 2}   # Vikram, Sam, Zoe not joined yet
    august = _summary(data, month="2026-08")["items"]
    assert {i["department"]: i["headcount"] for i in august} == {"Engineering": 2, "Marketing": 1, "Sales": 4, "Support": 2}
    assert all(i["present_days"] == 0.0 and i["avg_work_hours"] is None for i in august)   # nobody logged anything


def test_department_summary_filter_and_validation(data):
    sales = _summary(data, month="2026-07", department="Sales")["items"]
    assert [i["department"] for i in sales] == ["Sales"] and sales[0]["headcount"] == 3
    assert _summary(data, month="2026-07", department="Nope")["items"] == []
    assert data.get("/analytics/departments/summary").status_code == 422
    assert data.get("/analytics/departments/summary", params={"month": "2026-00"}).status_code == 422


# --------------------------------------------------------------------------- #
# late leaderboard
# --------------------------------------------------------------------------- #
VIKRAM = dict(rank=1, emp_code="EMP0002", name="Vikram", department="Engineering", total_late_minutes=60, late_count=1)
NEHA = dict(rank=2, emp_code="EMP0003", name="Neha", department="Sales", total_late_minutes=35, late_count=1)
ASHA = dict(rank=3, emp_code="EMP0001", name="Asha", department="Engineering", total_late_minutes=20, late_count=1)
ROHIT = dict(rank=3, emp_code="EMP0004", name="Rohit", department="Sales", total_late_minutes=20, late_count=1)


def _board(client, **params):
    r = client.get("/analytics/leaderboard/late", params=params)
    assert r.status_code == 200, r.text
    return r.json()


def test_leaderboard_ranks_ties_and_ignores_unknown_employees(data):
    result = _board(data, month="2026-07")
    assert result["month"] == "2026-07"
    # standard competition ranking: 1, 2, 3, 3; tied rows ordered by emp_code; EMP9999 (99 late) is ignored
    assert result["items"] == [VIKRAM, NEHA, ASHA, ROHIT]


def test_leaderboard_limit_applies_after_ranking(data):
    assert _board(data, month="2026-07", limit=1)["items"] == [VIKRAM]
    assert _board(data, month="2026-07", limit=2)["items"] == [VIKRAM, NEHA]
    # a tie at the cutoff: both rank-3 employees are returned, so there are more rows than the limit
    assert _board(data, month="2026-07", limit=3)["items"] == [VIKRAM, NEHA, ASHA, ROHIT]


def test_leaderboard_department_ranks_inside_the_department(data):
    items = _board(data, month="2026-07", department="Sales")["items"]
    assert items == [dict(NEHA, rank=1), dict(ROHIT, rank=2)]
    assert _board(data, month="2026-07", department="Support")["items"] == []     # nobody was late


def test_leaderboard_empty_month_and_validation(data):
    assert _board(data, month="2026-08")["items"] == []
    for params in ({"month": "2026-07", "limit": 0}, {"month": "2026-07", "limit": 51}, {"month": "bad"}, {}):
        assert data.get("/analytics/leaderboard/late", params=params).status_code == 422


# --------------------------------------------------------------------------- #
# department trend
# --------------------------------------------------------------------------- #
def _trend(client, department, start, end):
    r = client.get(f"/analytics/departments/{department}/trend", params={"from": start, "to": end})
    return r


def _rows(response):
    assert response.status_code == 200, response.text
    return response.json()["items"]


def test_trend_sales_with_weekends_gaps_and_moving_average(data):
    rows = _rows(_trend(data, "Sales", "2026-07-03", "2026-07-12"))
    assert [r["date"] for r in rows] == [f"2026-07-{d:02d}" for d in range(3, 13)]      # one row per calendar day
    assert [r["is_working_day"] for r in rows] == [True, False, False, True, True, True, True, True, False, False]
    assert [r["headcount"] for r in rows] == [2] * 10                      # Sam joins Jul 20, Zoe in August
    assert [r["present_count"] for r in rows] == [0, 0, 0, 1, 0.5, 0, 1, 0, 1, 0]       # Sat Jul 11 counts here
    assert [r["late_count"] for r in rows] == [0, 0, 0, 1, 0, 0, 0, 0, 1, 0]
    # rate only on working days; Fri Jul 3 and Fri Jul 10 have no records and still get 0.0, not null
    assert [r["attendance_rate"] for r in rows] == [0.0, None, None, 0.5, 0.25, 0.0, 0.5, 0.0, None, None]
    # mean of the non-null rates among this row and up to 6 before it, inside the range only
    assert [r["moving_avg_7d"] for r in rows] == [0.0, 0.0, 0.0, 0.25, 0.25, 0.1875, 0.25, 0.25, 0.25, 0.25]


def test_trend_headcount_changes_when_someone_joins_mid_range(data):
    rows = _rows(_trend(data, "Engineering", "2026-07-10", "2026-07-15"))
    assert [r["headcount"] for r in rows] == [1, 1, 1, 2, 2, 2]               # Vikram joins Mon Jul 13
    assert [r["attendance_rate"] for r in rows] == [1.0, None, None, 0.0, 0.5, 0.5]
    assert [r["present_count"] for r in rows] == [1, 0, 0, 0, 1, 1]
    assert [r["late_count"] for r in rows] == [1, 0, 0, 0, 0, 1]
    assert [r["moving_avg_7d"] for r in rows] == [1.0, 1.0, 1.0, 0.5, 0.5, 0.5]


def test_trend_department_with_no_logs_and_single_day_range(data):
    rows = _rows(_trend(data, "Marketing", "2026-07-06", "2026-07-08"))
    assert [(r["headcount"], r["present_count"], r["attendance_rate"], r["moving_avg_7d"]) for r in rows] == [
        (1, 0, 0.0, 0.0)] * 3
    one = _rows(_trend(data, "Marketing", "2026-07-11", "2026-07-11"))         # a Saturday
    assert len(one) == 1 and one[0]["attendance_rate"] is None and one[0]["moving_avg_7d"] is None
    assert one[0]["is_working_day"] is False


def test_trend_range_limit_counts_rows(data):
    ok = _rows(_trend(data, "Sales", "2026-07-01", "2026-09-30"))              # 92 rows
    expected = [(date(2026, 7, 1) + timedelta(days=i)).isoformat() for i in range(92)]
    assert [r["date"] for r in ok] == expected
    assert _trend(data, "Sales", "2026-07-01", "2026-10-01").status_code == 422   # 93 rows


def test_trend_errors(data):
    assert _trend(data, "Nope", "2026-07-01", "2026-07-05").status_code == 404
    assert _trend(data, "Sales", "2026-07-05", "2026-07-01").status_code == 422
    assert _trend(data, "Sales", "2026-02-30", "2026-07-01").status_code == 422
    assert data.get("/analytics/departments/Sales/trend", params={"from": "2026-07-01"}).status_code == 422
