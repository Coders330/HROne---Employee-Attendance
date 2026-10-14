# Employee Attendance & Analytics API

FastAPI + MongoDB (6.0 or newer). All code is in `app/main.py`.

## Run

```bash
python -m venv .venv && source .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install -r requirements.txt
export MONGO_URI="mongodb+srv://USER:PASSWORD@cluster0.xxxxx.mongodb.net/"   # or mongodb://localhost:27017
export MONGO_DB="attendance"
uvicorn app.main:app --port 8000
```

Windows PowerShell: `$env:MONGO_URI="..."` and `$env:MONGO_DB="attendance"`. A local `.env` file also works (see `.env.example`); real environment variables always win. Never commit `.env`.

Open `http://localhost:8000/health`. It returns 503 if MongoDB cannot be reached. Indexes are created automatically at startup. API docs: `http://localhost:8000/docs`.

## Tests

```bash
pip install -r requirements-dev.txt
export TEST_MONGO_DB="attendance_api_test"    # the tests EMPTY this database. Never use real data.
pytest
```

- `tests/test_rules.py` runs without MongoDB. The other tests are skipped if MongoDB is not reachable.
- `tests/test_explain.py` loads about 100,000 records and checks that the query plans use indexes and that endpoints answer quickly.

## Notes

- Times go in and out as epoch milliseconds, and are stored as UTC. Attendance dates and shifts follow IST (UTC+05:30).
- Money-like rounding (work hours, percentages) uses integer half-up arithmetic in Python, because MongoDB `$round` rounds half to even.
- Decisions on points the spec leaves open are listed in `DECISIONS.md` and in the comments of `app/main.py`.
