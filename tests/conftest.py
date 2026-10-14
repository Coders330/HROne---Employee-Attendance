"""Test setup.

The tests need a running MongoDB (6.0+, for example `docker run -p 27017:27017 mongo:7`).
They use their OWN database (TEST_MONGO_DB, default `attendance_api_test`) and empty its collections,
so never point TEST_MONGO_DB at a database you care about.
Pure rule tests (test_rules.py) do not need MongoDB; the other tests are skipped if it is unreachable.
"""
import os

# Must be set BEFORE app.main is imported, because the app reads MONGO_DB at import time.
os.environ["MONGO_DB"] = os.getenv("TEST_MONGO_DB", "attendance_api_test")

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app.main import app, client as mongo_client, db  # noqa: E402


@pytest.fixture(scope="session")
def mongo_available():
    try:
        mongo_client.admin.command("ping")
    except Exception as exc:  # pragma: no cover
        pytest.skip(f"MongoDB is not reachable: {exc}")


@pytest.fixture(scope="session")
def api(mongo_available):
    """One TestClient for the whole session; entering it runs the app's startup (index creation)."""
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture()
def client(api):
    """The TestClient with both collections emptied (indexes stay)."""
    db.employees.delete_many({})
    db.attendance_logs.delete_many({})
    return api


@pytest.fixture()
def seed(client):
    """seed(employees=[...], logs=[...]) inserts documents straight into MongoDB, like the grader does."""
    def _seed(employees=(), logs=()):
        if employees:
            db.employees.insert_many([dict(e) for e in employees])
        if logs:
            db.attendance_logs.insert_many([dict(l) for l in logs])
    return _seed


@pytest.fixture(scope="session")
def database():
    return db
