"""Pure rule tests (R1-R5, R8): no database, no HTTP."""
from datetime import date, datetime, timedelta, timezone

import pytest

from app.main import (
    attendance_date, count_weekdays, derive, div_half_up, dt_to_ms, late_minutes, month_bounds,
    ms_to_dt, overtime_minutes, work_cents,
)
from helpers import ist

UTC = timezone.utc
DAY_EMP = {"shift_start": "09:30", "shift_end": "18:30"}
NIGHT_EMP = {"shift_start": "22:00", "shift_end": "06:00"}


@pytest.mark.parametrize("hh,mm,ss,expected", [
    (9, 40, 0, 0),      # exactly 10:00 after the start is still inside the grace period
    (9, 40, 1, 10),     # one second later it is late, counted from shift start (not from the end of grace)
    (10, 15, 59, 45),
    (9, 0, 0, 0),       # early is never negative
    (9, 30, 0, 0),
])
def test_late_minutes_r2(hh, mm, ss, expected):
    assert late_minutes(ist("2026-07-06", f"{hh:02d}:{mm:02d}", ss), "2026-07-06", "09:30") == expected


@pytest.mark.parametrize("out_hhmm,expected", [("18:59", 0), ("19:00", 30), ("19:10", 40), ("18:30", 0), ("17:00", 0)])
def test_overtime_r3(out_hhmm, expected):
    assert overtime_minutes(ist("2026-07-06", out_hhmm), "2026-07-06", "09:30", "18:30") == expected


@pytest.mark.parametrize("seconds,cents", [
    (16182, 450),   # 4.495 hours rounds half-up to 4.50 (float round() would say 4.49)
    (16181, 449),
    (54, 2),        # 0.015 -> 0.02
    (53, 1),
    (0, 0),
    (3600, 100),
])
def test_work_cents_r4_half_up(seconds, cents):
    start = datetime(2026, 7, 6, 4, 0, tzinfo=UTC)
    assert work_cents(start, start + timedelta(seconds=seconds)) == cents


def test_half_day_uses_rounded_hours_r5():
    start = datetime(2026, 7, 6, 4, 0, tzinfo=UTC)
    full = derive(DAY_EMP, "2026-07-06", "PRESENT", start, start + timedelta(seconds=16182))
    half = derive(DAY_EMP, "2026-07-06", "PRESENT", start, start + timedelta(seconds=16181))
    assert (full["work_hours"], full["half_day"]) == (4.5, False)
    assert (half["work_hours"], half["half_day"]) == (4.49, True)


def test_derived_values_match_the_documented_samples():
    u = lambda *a: datetime(*a, tzinfo=UTC)
    s1 = derive(DAY_EMP, "2026-07-06", "PRESENT", u(2026, 7, 6, 3, 58), u(2026, 7, 6, 13, 5))
    assert (s1["work_hours"], s1["late_minutes"], s1["overtime_minutes"], s1["half_day"]) == (9.12, 0, 0, False)
    s2 = derive(DAY_EMP, "2026-07-06", "PRESENT", u(2026, 7, 6, 4, 35), u(2026, 7, 6, 13, 40))
    assert (s2["work_hours"], s2["late_minutes"], s2["overtime_minutes"]) == (9.08, 35, 40)
    s4 = derive(DAY_EMP, "2026-07-07", "WFH", u(2026, 7, 7, 4, 0), u(2026, 7, 7, 7, 30))
    assert (s4["work_hours"], s4["half_day"]) == (3.5, True)
    s7 = derive(NIGHT_EMP, "2026-07-06", "PRESENT", u(2026, 7, 6, 16, 25), u(2026, 7, 7, 1, 10))
    assert (s7["work_hours"], s7["late_minutes"], s7["overtime_minutes"]) == (8.75, 0, 40)


def test_derive_for_open_and_non_presence_records():
    start = datetime(2026, 7, 7, 4, 5, tzinfo=UTC)
    open_rec = derive(DAY_EMP, "2026-07-07", "PRESENT", start, None)
    assert open_rec == {"work_hours": None, "late_minutes": 0, "overtime_minutes": 0, "half_day": False}
    for status in ("ABSENT", "LEAVE"):
        assert derive(DAY_EMP, "2026-07-08", status, None, None) == {
            "work_hours": None, "late_minutes": 0, "overtime_minutes": 0, "half_day": False}


@pytest.mark.parametrize("punch_in,expected_date", [
    (ist("2026-07-06", "21:55"), "2026-07-06"),
    (ist("2026-07-07", "00:02"), "2026-07-06"),     # earlier than 06:00 -> the shift that started yesterday
    (ist("2026-07-07", "05:59", 59), "2026-07-06"),
    (ist("2026-07-07", "06:00"), "2026-07-07"),     # not earlier than shift_end -> its own date
    (ist("2026-07-07", "12:00"), "2026-07-07"),
])
def test_attendance_date_overnight_r1(punch_in, expected_date):
    assert attendance_date(punch_in, "22:00", "06:00") == expected_date


def test_attendance_date_is_the_ist_date_not_the_utc_date():
    # 19:00Z on Jul 6 is 00:30 IST on Jul 7
    assert attendance_date(datetime(2026, 7, 6, 19, 0, tzinfo=UTC), "09:30", "18:30") == "2026-07-07"


def test_overnight_late_and_overtime_use_the_record_date():
    assert late_minutes(ist("2026-07-07", "00:02"), "2026-07-06", "22:00") == 122
    assert late_minutes(ist("2026-07-07", "05:00"), "2026-07-06", "22:00") == 420
    assert late_minutes(ist("2026-07-07", "12:00"), "2026-07-07", "22:00") == 0
    assert late_minutes(ist("2026-07-06", "22:10", 0), "2026-07-06", "22:00") == 0
    assert late_minutes(ist("2026-07-06", "22:12"), "2026-07-06", "22:00") == 12
    assert overtime_minutes(ist("2026-07-07", "06:40"), "2026-07-06", "22:00", "06:00") == 40


def test_epoch_conversion_is_exact_and_truncates_to_seconds():
    assert ms_to_dt(1783310280000).isoformat() == "2026-07-06T03:58:00+00:00"
    assert dt_to_ms(ms_to_dt(1783310280999)) == 1783310280000  # 09:28:00.999 counts as 09:28:00


def test_months_and_weekdays_r7():
    assert month_bounds("2026-07") == ("2026-07-01", "2026-07-31")
    assert month_bounds("2028-02") == ("2028-02-01", "2028-02-29")
    assert count_weekdays(date(2026, 7, 1), date(2026, 7, 31)) == 23
    assert count_weekdays(date(2026, 7, 13), date(2026, 7, 31)) == 15   # a mid-month joiner counts from joined_on
    assert count_weekdays(date(2026, 8, 1), date(2026, 7, 31)) == 0     # joined after the month


def test_half_up_rounding_r8():
    assert div_half_up(1667, 2) == 834      # 833.5 -> 834 (banker's rounding would give 834 too, 832.5 would not)
    assert div_half_up(1665, 2) == 833      # 832.5 -> 833, banker's rounding would give 832
    assert div_half_up(1, 8) == 0
    assert div_half_up(5, 10) == 1
