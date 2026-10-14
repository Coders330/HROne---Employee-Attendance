"""Race-condition tests: parallel requests must be settled by the database, not by check-then-write."""
from collections import Counter
from concurrent.futures import ThreadPoolExecutor

from fastapi.testclient import TestClient

from app.main import app
from helpers import emp_payload, ist, ms

WORKERS = 20


def _in_parallel(fn, n=WORKERS):
    """Run fn(i) in n threads at (nearly) the same moment; each thread gets its own TestClient."""
    def run(i):
        return fn(TestClient(app), i)
    with ThreadPoolExecutor(max_workers=n) as pool:
        return list(pool.map(run, range(n)))


def _codes(responses):
    return Counter(r.status_code for r in responses)


def test_parallel_employee_create_gives_exactly_one_201(client):
    responses = _in_parallel(lambda c, i: c.post("/employees", json=emp_payload("EMP0001")))
    assert _codes(responses) == Counter({201: 1, 409: WORKERS - 1})


def test_parallel_punch_in_gives_exactly_one_201(client):
    client.post("/employees", json=emp_payload("EMP0001"))
    body = {"emp_code": "EMP0001", "punched_at": ms(ist("2026-07-06", "09:30"))}
    responses = _in_parallel(lambda c, i: c.post("/attendance/punch-in", json=body))
    assert _codes(responses) == Counter({201: 1, 409: WORKERS - 1})
    assert client.get("/attendance", params={"emp_code": "EMP0001"}).json()["total"] == 1


def test_parallel_punch_out_gives_exactly_one_200(client):
    client.post("/employees", json=emp_payload("EMP0001"))
    client.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": ms(ist("2026-07-06", "09:30"))})
    body = {"emp_code": "EMP0001", "punched_at": ms(ist("2026-07-06", "18:30"))}
    responses = _in_parallel(lambda c, i: c.post("/attendance/punch-out", json=body))
    assert _codes(responses) == Counter({200: 1, 409: WORKERS - 1})


def test_parallel_patches_never_lose_a_history_entry(client):
    client.post("/employees", json=emp_payload("EMP0001"))
    client.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": ms(ist("2026-07-06", "10:00"))})

    def patch(c, i):
        # every request asks for a different punch_in, so every successful one is a real change
        return c.patch("/attendance/EMP0001/2026-07-06", json={
            "punch_in": ms(ist("2026-07-06", "09:00", seconds=i + 1)),
            "reason": f"correction number {i}", "regularized_by": f"hr.{i}",
        })

    responses = _in_parallel(patch, n=10)
    codes = _codes(responses)
    assert set(codes) <= {200, 409} and codes[200] >= 1          # a loser may get 409, but never a 500
    record = client.get("/attendance", params={"emp_code": "EMP0001"}).json()["items"][0]
    assert len(record["history"]) == codes[200]                   # one entry per successful PATCH, none lost
    assert len({h["by"] for h in record["history"]}) == codes[200]
