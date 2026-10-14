"""Endpoint tests: employees, punch-in, punch-out, attendance list, PATCH (needs MongoDB)."""
import pytest

from helpers import emp_doc, emp_payload, ist, log_doc, ms


# --------------------------------------------------------------------------- #
# health and employees
# --------------------------------------------------------------------------- #
def test_health_ok(client):
    r = client.get("/health")
    assert r.status_code == 200 and r.json() == {"status": "ok"}


def test_create_employee_returns_ms_and_hides_id(client):
    r = client.post("/employees", json=emp_payload("EMP0001"))
    assert r.status_code == 201
    body = r.json()
    assert "_id" not in body and "id" not in body
    assert isinstance(body["created_at"], int) and body["created_at"] > 100_000_000_000
    assert body["shift_start"] == "09:30" and body["shift_end"] == "18:30"


def test_created_at_sent_by_a_client_is_ignored(client):
    r = client.post("/employees", json=emp_payload("EMP0001", created_at=1))
    assert r.status_code == 201 and r.json()["created_at"] != 1


def test_duplicate_emp_code_is_409(client):
    assert client.post("/employees", json=emp_payload("EMP0001")).status_code == 201
    assert client.post("/employees", json=emp_payload("EMP0001")).status_code == 409


@pytest.mark.parametrize("overrides", [
    {"emp_code": "EMP12"},                 # too short
    {"emp_code": "XYZ0001"},
    {"email": "not-an-email"},
    {"shift_start": "25:00"},
    {"shift_start": "09:30", "shift_end": "09:30"},   # must differ
    {"joined_on": "2026-02-30"},            # matches the pattern but is not a real date
    {"joined_on": "2026/07/01"},
    {"name": ""},
])
def test_create_employee_validation_is_422_with_a_list(client, overrides):
    r = client.post("/employees", json=emp_payload(**overrides))
    assert r.status_code == 422 and isinstance(r.json()["detail"], list)


def test_list_employees_pagination_filter_and_order(client, seed):
    seed(employees=[
        emp_doc("EMP0003", "Neha", "Sales", "2026-02-01"),
        emp_doc("EMP0001", "Asha", "Engineering", "2026-01-05"),
        emp_doc("EMP0002", "Vikram", "Engineering", "2026-07-13"),
        emp_doc("EMP0004", "Rohit", "Sales", "2026-02-01"),
    ])
    r = client.get("/employees", params={"page": 1, "page_size": 2}).json()
    assert [e["emp_code"] for e in r["items"]] == ["EMP0001", "EMP0002"]   # page 1 is the FIRST page
    assert r["total"] == 4
    r = client.get("/employees", params={"page": 2, "page_size": 2}).json()
    assert [e["emp_code"] for e in r["items"]] == ["EMP0003", "EMP0004"]
    r = client.get("/employees", params={"department": "Sales"}).json()
    assert r["total"] == 2 and [e["emp_code"] for e in r["items"]] == ["EMP0003", "EMP0004"]   # total follows the filter
    assert client.get("/employees", params={"department": "sales"}).json()["total"] == 0        # case-sensitive
    assert client.get("/employees", params={"page": 99}).json()["items"] == []


@pytest.mark.parametrize("params", [{"page": 0}, {"page_size": 0}, {"page_size": 101}, {"page": "x"}])
def test_list_employees_bad_paging_is_422(client, params):
    assert client.get("/employees", params=params).status_code == 422


# --------------------------------------------------------------------------- #
# punch-in
# --------------------------------------------------------------------------- #
def _employees(client):
    client.post("/employees", json=emp_payload("EMP0001", name="Neha"))
    client.post("/employees", json=emp_payload("EMP0005", name="Meera", department="Support",
                                               shift_start="22:00", shift_end="06:00"))


def test_punch_in_late_example(client):
    _employees(client)
    r = client.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": ms(ist("2026-07-06", "10:05"))})
    assert r.status_code == 201
    body = r.json()
    assert body["date"] == "2026-07-06" and body["status"] == "PRESENT"
    assert body["late_minutes"] == 35 and body["overtime_minutes"] == 0 and body["half_day"] is False
    assert body["punch_out"] is None and body["work_hours"] is None and body["history"] == []
    assert body["punch_in"] == ms(ist("2026-07-06", "10:05"))
    assert "id" not in body and "_id" not in body


def test_punch_in_unknown_employee_is_404(client):
    r = client.post("/attendance/punch-in", json={"emp_code": "EMP9999", "punched_at": ms(ist("2026-07-06", "10:05"))})
    assert r.status_code == 404


def test_punch_in_truncates_to_whole_seconds_and_applies_grace(client):
    _employees(client)
    at = ms(ist("2026-07-06", "09:40", seconds=0, micro=900_000))   # 09:40:00.900
    r = client.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": at})
    assert r.status_code == 201
    assert r.json()["late_minutes"] == 0                    # counts as 09:40:00, exactly 10:00 after the start
    assert r.json()["punch_in"] == ms(ist("2026-07-06", "09:40"))   # the truncated value is what is returned


def test_punch_in_one_second_over_grace_counts_from_shift_start(client):
    _employees(client)
    r = client.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": ms(ist("2026-07-06", "09:40", 1))})
    assert r.json()["late_minutes"] == 10


def test_punch_in_status_values(client):
    _employees(client)
    ok = client.post("/attendance/punch-in", json={"emp_code": "EMP0001", "status": "WFH",
                                                   "punched_at": ms(ist("2026-07-06", "09:30"))})
    assert ok.status_code == 201 and ok.json()["status"] == "WFH"
    for bad in ("ABSENT", "LEAVE", "X", "present"):
        r = client.post("/attendance/punch-in", json={"emp_code": "EMP0001", "status": bad,
                                                      "punched_at": ms(ist("2026-07-07", "09:30"))})
        assert r.status_code == 422, bad


@pytest.mark.parametrize("bad", [1783310280, 1783310280000.5, "1783310280000", True, 0, -5, 99_999_999_999,
                                 4_102_444_800_001])
def test_punch_in_bad_timestamps_are_422(client, bad):
    _employees(client)
    r = client.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": bad})
    assert r.status_code == 422 and isinstance(r.json()["detail"], list)


def test_punch_in_without_time_uses_now_and_null_is_the_same(client):
    _employees(client)
    assert client.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": None}).status_code == 201


def test_second_punch_in_same_date_is_409(client):
    _employees(client)
    body = {"emp_code": "EMP0001", "punched_at": ms(ist("2026-07-06", "09:30"))}
    assert client.post("/attendance/punch-in", json=body).status_code == 201
    assert client.post("/attendance/punch-in", json={**body, "punched_at": ms(ist("2026-07-06", "11:00"))}).status_code == 409


def test_punch_in_overnight_date_rule(client):
    _employees(client)
    r = client.post("/attendance/punch-in", json={"emp_code": "EMP0005", "punched_at": ms(ist("2026-07-07", "00:02"))})
    assert r.status_code == 201
    assert r.json()["date"] == "2026-07-06" and r.json()["late_minutes"] == 122


def test_punch_in_date_is_the_ist_date(client):
    _employees(client)
    at = ms(ist("2026-07-07", "00:30"))     # 19:00Z on Jul 6
    r = client.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": at})
    assert r.json()["date"] == "2026-07-07"


# --------------------------------------------------------------------------- #
# punch-out
# --------------------------------------------------------------------------- #
def _punch_in(client, code, day, hhmm):
    r = client.post("/attendance/punch-in", json={"emp_code": code, "punched_at": ms(ist(day, hhmm))})
    assert r.status_code == 201, r.text
    return r.json()


def test_punch_out_computes_hours_overtime_and_half_day(client):
    _employees(client)
    _punch_in(client, "EMP0001", "2026-07-06", "10:05")
    r = client.post("/attendance/punch-out", json={"emp_code": "EMP0001", "punched_at": ms(ist("2026-07-06", "19:10"))})
    assert r.status_code == 200
    body = r.json()
    assert body["work_hours"] == 9.08 and body["overtime_minutes"] == 40 and body["late_minutes"] == 35
    assert body["half_day"] is False and body["punch_out"] == ms(ist("2026-07-06", "19:10"))


@pytest.mark.parametrize("seconds,hours,half", [(16182, 4.5, False), (16181, 4.49, True)])
def test_punch_out_half_day_boundary_uses_half_up(client, seconds, hours, half):
    _employees(client)
    _punch_in(client, "EMP0001", "2026-07-06", "09:30")
    out = ms(ist("2026-07-06", "09:30")) + seconds * 1000
    r = client.post("/attendance/punch-out", json={"emp_code": "EMP0001", "punched_at": out})
    assert r.status_code == 200 and r.json()["work_hours"] == hours and r.json()["half_day"] is half


def test_punch_out_across_midnight_for_an_overnight_shift(client):
    _employees(client)
    _punch_in(client, "EMP0005", "2026-07-06", "21:55")
    r = client.post("/attendance/punch-out", json={"emp_code": "EMP0005",
                                                   "punched_at": ms(ist("2026-07-06", "06:40", plus_days=1))})
    assert r.status_code == 200
    body = r.json()
    assert body["date"] == "2026-07-06" and body["work_hours"] == 8.75 and body["overtime_minutes"] == 40


def test_punch_out_errors_and_check_order(client):
    _employees(client)
    out = {"emp_code": "EMP9999", "punched_at": ms(ist("2026-07-06", "19:00"))}
    assert client.post("/attendance/punch-out", json=out).status_code == 404            # unknown employee
    out["emp_code"] = "EMP0001"
    assert client.post("/attendance/punch-out", json=out).status_code == 404            # no punch-in found
    _punch_in(client, "EMP0001", "2026-07-06", "09:30")
    same = {"emp_code": "EMP0001", "punched_at": ms(ist("2026-07-06", "09:30"))}
    assert client.post("/attendance/punch-out", json=same).status_code == 422           # not after the punch-in
    too_late = {"emp_code": "EMP0001", "punched_at": ms(ist("2026-07-07", "09:30", 1))}
    assert client.post("/attendance/punch-out", json=too_late).status_code == 422       # more than 24 hours later
    exactly_24h = {"emp_code": "EMP0001", "punched_at": ms(ist("2026-07-07", "09:30"))}
    assert client.post("/attendance/punch-out", json=exactly_24h).status_code == 200    # 24:00:00 is still allowed
    # F7: already closed AND more than 24 hours away -> 409 (closed) wins over 422
    both = {"emp_code": "EMP0001", "punched_at": ms(ist("2026-07-10", "09:30"))}
    assert client.post("/attendance/punch-out", json=both).status_code == 409


def test_punch_out_twice_is_409(client):
    _employees(client)
    _punch_in(client, "EMP0001", "2026-07-06", "09:30")
    body = {"emp_code": "EMP0001", "punched_at": ms(ist("2026-07-06", "18:30"))}
    assert client.post("/attendance/punch-out", json=body).status_code == 200
    assert client.post("/attendance/punch-out", json=body).status_code == 409


# --------------------------------------------------------------------------- #
# list attendance
# --------------------------------------------------------------------------- #
def _seed_list(seed):
    seed(
        employees=[emp_doc("EMP0001", "Asha", "Engineering", "2026-01-05"),
                   emp_doc("EMP0002", "Vikram", "Engineering", "2026-07-13")],
        logs=[
            log_doc("EMP0002", "2026-07-14", "PRESENT", ist("2026-07-14", "09:32"), ist("2026-07-14", "18:31"),
                    work_hours=8.98, legacy=True),
            log_doc("EMP0001", "2026-07-14", "WFH", ist("2026-07-14", "09:30"), ist("2026-07-14", "13:00"),
                    work_hours=3.5, half_day=True),
            log_doc("EMP0001", "2026-07-06", "PRESENT", ist("2026-07-06", "09:28"), ist("2026-07-06", "18:35"),
                    work_hours=9.12),
            log_doc("EMP0001", "2026-07-08", "LEAVE"),
        ],
    )


def test_list_attendance_order_filters_totals(client, seed):
    _seed_list(seed)
    r = client.get("/attendance").json()
    assert r["total"] == 4
    # date descending, then emp_code ascending
    assert [(i["date"], i["emp_code"]) for i in r["items"]] == [
        ("2026-07-14", "EMP0001"), ("2026-07-14", "EMP0002"), ("2026-07-08", "EMP0001"), ("2026-07-06", "EMP0001")]
    r = client.get("/attendance", params={"emp_code": "EMP0001", "page_size": 2}).json()
    assert r["total"] == 3 and len(r["items"]) == 2               # total reflects the filter, not the page
    r = client.get("/attendance", params={"date_from": "2026-07-08", "date_to": "2026-07-14"}).json()
    assert r["total"] == 3                                        # inclusive on both ends
    r = client.get("/attendance", params={"status": "LEAVE"}).json()
    assert r["total"] == 1 and r["items"][0]["status"] == "LEAVE"
    r = client.get("/attendance", params={"page": 2, "page_size": 3}).json()
    assert r["total"] == 4 and len(r["items"]) == 1


def test_list_attendance_shape_and_legacy_defaults(client, seed):
    _seed_list(seed)
    items = client.get("/attendance", params={"emp_code": "EMP0002"}).json()["items"]
    assert len(items) == 1
    rec = items[0]
    assert "_id" not in rec and "id" not in rec
    assert rec["half_day"] is False and rec["history"] == []        # legacy record: missing means false / empty
    assert isinstance(rec["punch_in"], int) and rec["work_hours"] == 8.98
    leave = client.get("/attendance", params={"status": "LEAVE"}).json()["items"][0]
    assert leave["punch_in"] is None and leave["punch_out"] is None and leave["work_hours"] is None


@pytest.mark.parametrize("params", [
    {"date_from": "2026-07-10", "date_to": "2026-07-01"},
    {"date_from": "2026-02-30"}, {"date_to": "07/10/2026"}, {"status": "BOGUS"}, {"page": 0}, {"page_size": 500},
])
def test_list_attendance_validation_is_422(client, params):
    assert client.get("/attendance", params=params).status_code == 422


# --------------------------------------------------------------------------- #
# PATCH (regularize)
# --------------------------------------------------------------------------- #
def _closed_record(client):
    _employees(client)
    _punch_in(client, "EMP0001", "2026-07-07", "10:05")
    r = client.post("/attendance/punch-out", json={"emp_code": "EMP0001", "punched_at": ms(ist("2026-07-07", "18:35"))})
    assert r.status_code == 200


def _patch(client, code="EMP0001", day="2026-07-07", **body):
    body.setdefault("reason", "biometric glitch")
    body.setdefault("regularized_by", "hr.admin")
    return client.patch(f"/attendance/{code}/{day}", json=body)


def test_patch_recomputes_and_appends_one_history_entry(client):
    _closed_record(client)
    r = _patch(client, punch_in=ms(ist("2026-07-07", "09:28")))
    assert r.status_code == 200
    body = r.json()
    assert body["late_minutes"] == 0 and body["work_hours"] == 9.12 and body["half_day"] is False
    assert body["punch_in"] == ms(ist("2026-07-07", "09:28"))
    assert len(body["history"]) == 1
    entry = body["history"][0]
    assert entry["by"] == "hr.admin" and entry["reason"] == "biometric glitch" and isinstance(entry["at"], int)
    assert entry["changes"] == {                                  # only fields that actually changed, times in ms
        "punch_in": {"from": ms(ist("2026-07-07", "10:05")), "to": ms(ist("2026-07-07", "09:28"))},
        "late_minutes": {"from": 35, "to": 0},
        "work_hours": {"from": 8.5, "to": 9.12},
    }
    second = _patch(client, punch_out=ms(ist("2026-07-07", "19:00")), reason="late checkout approved")
    assert second.status_code == 200 and len(second.json()["history"]) == 2     # appended, never overwritten
    assert second.json()["history"][0] == entry
    assert second.json()["overtime_minutes"] == 30


def test_patch_ignores_derived_fields_sent_by_a_client(client):
    _closed_record(client)
    r = _patch(client, punch_in=ms(ist("2026-07-07", "09:28")), work_hours=99, late_minutes=77, half_day=True)
    assert r.status_code == 200 and r.json()["work_hours"] == 9.12 and r.json()["late_minutes"] == 0


def test_patch_that_changes_nothing_is_422(client):
    _closed_record(client)
    assert _patch(client, punch_in=ms(ist("2026-07-07", "10:05"))).status_code == 422
    assert _patch(client, status="PRESENT").status_code == 422


def test_patch_404s_and_body_validation(client):
    _closed_record(client)
    assert _patch(client, code="EMP9999", punch_in=ms(ist("2026-07-07", "09:00"))).status_code == 404
    assert _patch(client, day="2026-07-09", punch_in=ms(ist("2026-07-09", "09:00"))).status_code == 404
    assert _patch(client, day="2026-02-30", punch_in=ms(ist("2026-07-07", "09:00"))).status_code == 422
    assert _patch(client, punch_in=ms(ist("2026-07-07", "09:00")), reason="abc").status_code == 422
    assert _patch(client, punch_in=1783310280).status_code == 422
    r = client.patch("/attendance/EMP0001/2026-07-07", json={"punch_in": ms(ist("2026-07-07", "09:00"))})
    assert r.status_code == 422                                   # reason and regularized_by are required


def test_patch_time_rules(client):
    _closed_record(client)
    assert _patch(client, punch_in=ms(ist("2026-07-08", "09:30"))).status_code == 422      # leaves the record's date
    assert _patch(client, punch_out=ms(ist("2026-07-07", "09:00"))).status_code == 422     # before punch_in
    assert _patch(client, punch_out=ms(ist("2026-07-08", "10:06"))).status_code == 422     # more than 24h after
    assert _patch(client, punch_in=ms(ist("2026-07-07", "19:00"))).status_code == 422      # after the stored punch_out


def test_patch_absent_and_leave_rules(client):
    _closed_record(client)
    assert _patch(client, status="LEAVE", punch_in=ms(ist("2026-07-07", "09:00"))).status_code == 422
    assert _patch(client, status="ABSENT", punch_out=ms(ist("2026-07-07", "18:00"))).status_code == 422
    r = _patch(client, status="ABSENT")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ABSENT" and body["punch_in"] is None and body["punch_out"] is None
    assert (body["work_hours"], body["late_minutes"], body["overtime_minutes"], body["half_day"]) == (None, 0, 0, False)
    # overtime was already 0 and half_day already False, so only the fields that really changed are listed
    assert set(body["history"][0]["changes"]) == {"status", "punch_in", "punch_out", "work_hours", "late_minutes"}


def test_patch_punch_in_on_absent_record_switches_to_present_f10(seed, client):
    seed(employees=[emp_doc("EMP0004", "Rohit", "Sales", "2026-02-01")],
         logs=[log_doc("EMP0004", "2026-07-08", "ABSENT"), log_doc("EMP0004", "2026-07-09", "LEAVE")])
    r = _patch(client, code="EMP0004", day="2026-07-08", punch_in=ms(ist("2026-07-08", "09:50")))
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "PRESENT" and body["late_minutes"] == 20 and body["work_hours"] is None
    assert body["history"][0]["changes"]["status"] == {"from": "ABSENT", "to": "PRESENT"}
    # a punch_out alone cannot revive the record (no punch_in), and neither can a bare presence status
    assert _patch(client, code="EMP0004", day="2026-07-09", punch_out=ms(ist("2026-07-09", "18:00"))).status_code == 422
    assert _patch(client, code="EMP0004", day="2026-07-09", status="PRESENT").status_code == 422
    assert _patch(client, code="EMP0004", day="2026-07-09", status="WFH",
                  punch_in=ms(ist("2026-07-09", "09:30"))).status_code == 200


def test_patch_legacy_record_without_history(seed, client):
    seed(employees=[emp_doc("EMP0002", "Vikram", "Engineering", "2026-07-13")],
         logs=[log_doc("EMP0002", "2026-07-14", "PRESENT", ist("2026-07-14", "09:32"), ist("2026-07-14", "18:31"),
                       work_hours=8.98, legacy=True)])
    r = _patch(client, code="EMP0002", day="2026-07-14", punch_in=ms(ist("2026-07-14", "09:00")))
    assert r.status_code == 200 and len(r.json()["history"]) == 1 and r.json()["half_day"] is False
