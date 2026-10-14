"""
Employee Attendance & Analytics API (FastAPI + MongoDB).

Run:  uvicorn app.main:app --port 8000
Env:  MONGO_URI, MONGO_DB (a local .env is loaded; real environment variables always win)

Layout of this file (top to bottom):
  config -> rule engine (pure functions) -> validation -> serializers -> indexes
  -> query / pipeline builders -> endpoints

Choices where the contract is silent (also written up in DECISIONS.md):
  * F7  punch-out check order: unknown employee 404, no record 404, already closed 409, time problems 422.
  * F8  trend range limit counts rows: more than 92 days listed (both ends included) gives 422.
  * F9  trend moving average is the mean of the already-rounded 4-decimal attendance_rate values.
  * F10 PATCH that sends punch_in on an ABSENT/LEAVE record without a status switches it to PRESENT.
"""
import calendar
import json
import logging
import os
import re
from contextlib import asynccontextmanager
from datetime import date, datetime, time as dtime, timedelta, timezone
from typing import Annotated, Any, Literal, Optional

from bson import json_util
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Path, Query
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import AfterValidator, BaseModel, ConfigDict, Field, StrictInt, model_validator
from pymongo import MongoClient, ReturnDocument
from pymongo.errors import DuplicateKeyError, PyMongoError, ServerSelectionTimeoutError

load_dotenv()  # python-dotenv does not override variables that are already set in the environment

log = logging.getLogger("attendance")

client = MongoClient(
    os.getenv("MONGO_URI", "mongodb://localhost:27017"),
    tz_aware=True,  # datetimes come back timezone-aware (UTC) instead of naive
    serverSelectionTimeoutMS=int(os.getenv("MONGO_TIMEOUT_MS", "5000")),
)
db = client[os.getenv("MONGO_DB", "attendance_db")]

# --------------------------------------------------------------------------- #
# RULES START  (pure functions, standard library only: no database, no FastAPI)
# --------------------------------------------------------------------------- #
UTC = timezone.utc
IST = timezone(timedelta(hours=5, minutes=30))
EPOCH = datetime(1970, 1, 1, tzinfo=UTC)

MS_MIN, MS_MAX = 100_000_000_000, 4_102_444_800_000
PRESENCE = ("PRESENT", "WFH", "ON_DUTY")
STATUSES = ("PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY")
GRACE_SECONDS = 600          # R2: late only if strictly more than 10:00 after shift start
OVERTIME_MIN_MINUTES = 30    # R3
HALF_DAY_CENTS = 450         # R5: rounded work hours below 4.50
MAX_TREND_DAYS = 92          # F8: counted as rows (both ends included)


def utc_now() -> datetime:
    """Current instant truncated to whole seconds (R1)."""
    return datetime.now(UTC).replace(microsecond=0)


def utc_now_ms() -> datetime:
    """Current instant at millisecond precision (what MongoDB stores)."""
    now = datetime.now(UTC)
    return now.replace(microsecond=(now.microsecond // 1000) * 1000)


def ms_to_dt(ms: int) -> datetime:
    """Epoch milliseconds to an aware UTC datetime, truncated to whole seconds (R1). Exact integer math."""
    return (EPOCH + timedelta(milliseconds=ms)).replace(microsecond=0)


def aware(dt: Optional[datetime]) -> Optional[datetime]:
    if dt is not None and dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    return dt


def dt_to_ms(dt: Optional[datetime]) -> Optional[int]:
    if dt is None:
        return None
    return (aware(dt) - EPOCH) // timedelta(milliseconds=1)


def _hm(text: str) -> tuple:
    hour, minute = text.split(":")
    return int(hour), int(minute)


def is_overnight(shift_start: str, shift_end: str) -> bool:
    return _hm(shift_end) <= _hm(shift_start)


def attendance_date(punch_in: datetime, shift_start: str, shift_end: str) -> str:
    """R1: IST calendar date of the punch-in; for an overnight shift a punch-in earlier than shift_end
    belongs to the shift that started the previous day."""
    local = aware(punch_in).astimezone(IST)
    day = local.date()
    if is_overnight(shift_start, shift_end):
        end_h, end_m = _hm(shift_end)
        if local.time() < dtime(end_h, end_m):
            day -= timedelta(days=1)
    return day.isoformat()


def shift_start_at(day: str, shift_start: str) -> datetime:
    d = date.fromisoformat(day)
    hour, minute = _hm(shift_start)
    return datetime(d.year, d.month, d.day, hour, minute, tzinfo=IST)


def shift_end_at(day: str, shift_start: str, shift_end: str) -> datetime:
    """The shift end of an overnight shift falls on the next calendar day."""
    d = date.fromisoformat(day)
    if is_overnight(shift_start, shift_end):
        d += timedelta(days=1)
    hour, minute = _hm(shift_end)
    return datetime(d.year, d.month, d.day, hour, minute, tzinfo=IST)


def late_minutes(punch_in: datetime, day: str, shift_start: str) -> int:
    """R2: strictly more than 600 s after shift start -> whole minutes (floor) since shift start, else 0."""
    seconds = int((aware(punch_in) - shift_start_at(day, shift_start)).total_seconds())
    return seconds // 60 if seconds > GRACE_SECONDS else 0


def overtime_minutes(punch_out: datetime, day: str, shift_start: str, shift_end: str) -> int:
    """R3: whole minutes (floor) past shift end, counted only when at least 30."""
    seconds = int((aware(punch_out) - shift_end_at(day, shift_start, shift_end)).total_seconds())
    minutes = seconds // 60 if seconds > 0 else 0
    return minutes if minutes >= OVERTIME_MIN_MINUTES else 0


def work_cents(punch_in: datetime, punch_out: datetime) -> int:
    """R4: work hours in hundredths, half-up, with integer math only.
    hours*100 = seconds/36, so half-up is (seconds + 18) // 36."""
    seconds = int((aware(punch_out) - aware(punch_in)).total_seconds())
    return (seconds + 18) // 36


def div_half_up(numerator: int, denominator: int) -> int:
    """Round numerator/denominator half-up to an integer (both non-negative, denominator > 0)."""
    return (2 * numerator + denominator) // (2 * denominator)


def derive(emp: dict, day: str, status: str, punch_in: Optional[datetime], punch_out: Optional[datetime]) -> dict:
    """The four stored derived fields (R2-R5) for a record."""
    if status in PRESENCE and punch_in is not None:
        late = late_minutes(punch_in, day, emp["shift_start"])
        if punch_out is None:
            return {"work_hours": None, "late_minutes": late, "overtime_minutes": 0, "half_day": False}
        cents = work_cents(punch_in, punch_out)
        return {
            "work_hours": cents / 100,
            "late_minutes": late,
            "overtime_minutes": overtime_minutes(punch_out, day, emp["shift_start"], emp["shift_end"]),
            "half_day": cents < HALF_DAY_CENTS,
        }
    return {"work_hours": None, "late_minutes": 0, "overtime_minutes": 0, "half_day": False}


def month_bounds(month: str) -> tuple:
    """('YYYY-MM-01', 'YYYY-MM-<last day>') for a 'YYYY-MM' string."""
    year, mon = int(month[:4]), int(month[5:7])
    last = calendar.monthrange(year, mon)[1]
    return f"{month}-01", f"{month}-{last:02d}"


def count_weekdays(start: date, end: date) -> int:
    """Mon-Fri days from start to end, both included (R7). 0 when end is before start."""
    count, day = 0, start
    while day <= end:
        if day.weekday() < 5:
            count += 1
        day += timedelta(days=1)
    return count
# --------------------------------------------------------------------------- #
# RULES END
# --------------------------------------------------------------------------- #


# --------------------------------------------------------------------------- #
# Validation
# --------------------------------------------------------------------------- #
HHMM_PATTERN = r"^([01][0-9]|2[0-3]):[0-5][0-9]$"
_DATE_RE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")
_MONTH_RE = re.compile(r"[0-9]{4}-(0[1-9]|1[0-2])")


def _valid_date(value: str) -> str:
    if not _DATE_RE.fullmatch(value):
        raise ValueError("must be a date in YYYY-MM-DD format")
    try:
        date.fromisoformat(value)
    except ValueError:
        raise ValueError("not a real calendar date")
    return value


def _valid_month(value: str) -> str:
    if not _MONTH_RE.fullmatch(value) or int(value[:4]) < 1:
        raise ValueError("must be a month in YYYY-MM format")
    return value


DateStr = Annotated[str, AfterValidator(_valid_date)]
MonthStr = Annotated[str, AfterValidator(_valid_month)]
# StrictInt rejects floats, numeric strings and booleans; the bounds reject seconds-looking values.
EpochMs = Annotated[StrictInt, Field(ge=MS_MIN, le=MS_MAX)]
Status = Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]
PresenceStatus = Literal["PRESENT", "WFH", "ON_DUTY"]
Page = Annotated[int, Query(ge=1)]
PageSize = Annotated[int, Query(ge=1, le=100)]


def unprocessable(loc: tuple, message: str):
    """422 with a list-shaped `detail`, like FastAPI's own validation errors."""
    raise RequestValidationError([{"type": "value_error", "loc": loc, "msg": message}])


class EmployeeIn(BaseModel):
    model_config = ConfigDict(extra="ignore")  # created_at or anything else a client sends is ignored

    emp_code: str = Field(pattern=r"^EMP[0-9]{4,6}$")
    name: str = Field(min_length=1, max_length=100)
    email: str = Field(max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department: str = Field(min_length=1, max_length=50)
    shift_start: str = Field(default="09:30", pattern=HHMM_PATTERN)
    shift_end: str = Field(default="18:30", pattern=HHMM_PATTERN)
    joined_on: DateStr

    @model_validator(mode="after")
    def _shift_times_differ(self):
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start must differ from shift_end")
        return self


class PunchInIn(BaseModel):
    model_config = ConfigDict(extra="ignore")

    emp_code: str = Field(min_length=1, max_length=50)
    punched_at: Optional[EpochMs] = None  # null or missing means "now"
    status: PresenceStatus = "PRESENT"


class PunchOutIn(BaseModel):
    model_config = ConfigDict(extra="ignore")

    emp_code: str = Field(min_length=1, max_length=50)
    punched_at: Optional[EpochMs] = None


class RegularizeIn(BaseModel):
    model_config = ConfigDict(extra="ignore")  # derived fields sent by a client are ignored

    status: Optional[Status] = None
    punch_in: Optional[EpochMs] = None
    punch_out: Optional[EpochMs] = None
    reason: str = Field(min_length=5, max_length=200)
    regularized_by: str = Field(min_length=1, max_length=50)


# --------------------------------------------------------------------------- #
# Serializers (BSON datetimes -> epoch milliseconds, defaults for legacy records, no _id)
# --------------------------------------------------------------------------- #
def ser_employee(doc: dict) -> dict:
    return {
        "emp_code": doc["emp_code"],
        "name": doc["name"],
        "email": doc["email"],
        "department": doc["department"],
        "shift_start": doc["shift_start"],
        "shift_end": doc["shift_end"],
        "joined_on": doc["joined_on"],
        "created_at": dt_to_ms(doc.get("created_at")),
    }


def _history_value(field: str, value: Any) -> Any:
    return dt_to_ms(value) if field in ("punch_in", "punch_out") and isinstance(value, datetime) else value


def ser_history(entry: dict) -> dict:
    changes = {}
    for field, pair in (entry.get("changes") or {}).items():
        changes[field] = {
            "from": _history_value(field, pair.get("from")),
            "to": _history_value(field, pair.get("to")),
        }
    return {"at": dt_to_ms(entry.get("at")), "by": entry.get("by"), "reason": entry.get("reason"), "changes": changes}


def ser_log(doc: dict) -> dict:
    return {
        "emp_code": doc["emp_code"],
        "date": doc["date"],
        "status": doc["status"],
        "punch_in": dt_to_ms(doc.get("punch_in")),
        "punch_out": dt_to_ms(doc.get("punch_out")),
        "work_hours": doc.get("work_hours"),
        "late_minutes": doc.get("late_minutes") or 0,
        "overtime_minutes": doc.get("overtime_minutes") or 0,
        "half_day": bool(doc.get("half_day", False)),
        "history": [ser_history(h) for h in (doc.get("history") or [])],
    }


# --------------------------------------------------------------------------- #
# Indexes (created at startup, idempotent: same name + same keys is a no-op)
# --------------------------------------------------------------------------- #
IDX_EMP_UNIQUE = "emp_code_unique"
IDX_EMP_DEPT_CODE = "department_emp_code"
IDX_EMP_DEPT_JOINED = "department_joined_on"
IDX_EMP_JOINED = "joined_on"
IDX_LOG_UNIQUE = "emp_code_date_unique"
IDX_LOG_DATE = "date_desc_emp_code"
IDX_LOG_STATUS = "status_date_desc_emp_code"

INDEXES = [
    ("employees", [("emp_code", 1)], IDX_EMP_UNIQUE, True),
    ("employees", [("department", 1), ("emp_code", 1)], IDX_EMP_DEPT_CODE, False),
    ("employees", [("department", 1), ("joined_on", 1)], IDX_EMP_DEPT_JOINED, False),
    ("employees", [("joined_on", 1)], IDX_EMP_JOINED, False),
    ("attendance_logs", [("emp_code", 1), ("date", 1)], IDX_LOG_UNIQUE, True),
    ("attendance_logs", [("date", -1), ("emp_code", 1)], IDX_LOG_DATE, False),
    ("attendance_logs", [("status", 1), ("date", -1), ("emp_code", 1)], IDX_LOG_STATUS, False),
]

_state = {"indexes_ready": False}


def ensure_indexes() -> bool:
    ready = True
    for collection, keys, name, unique in INDEXES:
        try:
            db[collection].create_index(keys, name=name, unique=unique)
        except ServerSelectionTimeoutError:
            log.error("MongoDB is not reachable; indexes will be retried by /health")
            return False
        except PyMongoError as exc:  # for example duplicate (emp_code, date) pairs in seeded data
            log.error("could not create index %s on %s: %s", name, collection, exc)
            ready = False
    return ready


@asynccontextmanager
async def lifespan(_app: FastAPI):
    _state["indexes_ready"] = ensure_indexes()
    yield


app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0", lifespan=lifespan)


@app.exception_handler(RequestValidationError)
async def validation_error_handler(_request, exc: RequestValidationError):
    errors = [
        {"type": err.get("type"), "loc": list(err.get("loc", ())), "msg": err.get("msg")}
        for err in exc.errors()
    ]
    return JSONResponse(status_code=422, content={"detail": errors})


@app.exception_handler(PyMongoError)
async def database_error_handler(_request, exc: PyMongoError):
    log.error("database error: %s", exc)
    return JSONResponse(status_code=503, content={"detail": "database unavailable"})


# --------------------------------------------------------------------------- #
# Query / pipeline builders. Each endpoint and its explain use the SAME builder.
# --------------------------------------------------------------------------- #
MAX_SKIP = 2 ** 40  # beyond this the page is certainly empty; avoids BSON integer overflow


def _skip_for(page: int, page_size: int) -> int:
    return (page - 1) * page_size


def build_attendance_list(emp_code, date_from, date_to, status, page, page_size) -> dict:
    if date_from is not None and date_to is not None and date_from > date_to:
        unprocessable(("query", "date_from"), "date_from must not be after date_to")
    query: dict = {}
    if emp_code is not None:
        query["emp_code"] = emp_code
    if date_from is not None or date_to is not None:
        query["date"] = {}
        if date_from is not None:
            query["date"]["$gte"] = date_from
        if date_to is not None:
            query["date"]["$lte"] = date_to
    if status is not None:
        query["status"] = status
    return {
        "collection": "attendance_logs",
        "kind": "find",
        "filter": query,
        "projection": {"_id": 0},
        "sort": {"date": -1, "emp_code": 1},
        "skip": _skip_for(page, page_size),
        "limit": page_size,
    }


def _in_presence(prefix: str) -> dict:
    return {"$in": [prefix + "status", list(PRESENCE)]}


def _late_of(prefix: str) -> dict:
    return {"$ifNull": [prefix + "late_minutes", 0]}


def _is_weekday(prefix: str) -> dict:
    dow = {"$dayOfWeek": {"$dateFromString": {"dateString": prefix + "date", "format": "%Y-%m-%d", "onError": None}}}
    return {"$and": [{"$gte": [dow, 2]}, {"$lte": [dow, 6]}]}  # $dayOfWeek: 1 = Sunday ... 7 = Saturday


def _present_halves(prefix: str, weekdays_only: bool) -> dict:
    """Present-day units in halves: full day = 2, half day = 1, not present = 0."""
    conditions = [_in_presence(prefix)]
    if weekdays_only:
        conditions.append(_is_weekday(prefix))
    half = {"$eq": [{"$ifNull": [prefix + "half_day", False]}, True]}
    return {"$cond": [{"$and": conditions}, {"$cond": [half, 1, 2]}, 0]}


def build_employee_monthly(emp_code: str, month: str) -> dict:
    first, last = month_bounds(month)
    pipeline = [
        {"$match": {"emp_code": emp_code, "date": {"$gte": first, "$lte": last}}},
        {"$group": {
            "_id": None,
            "present_halves": {"$sum": _present_halves("$", weekdays_only=True)},
            "leave_days": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
            "late_count": {"$sum": {"$cond": [{"$gt": [_late_of("$"), 0]}, 1, 0]}},
            "total_late_minutes": {"$sum": _late_of("$")},
            "total_overtime_minutes": {"$sum": {"$ifNull": ["$overtime_minutes", 0]}},
        }},
    ]
    return {"collection": "attendance_logs", "kind": "aggregate", "pipeline": pipeline, "hint": IDX_LOG_UNIQUE}


def _over_logs(expression: dict) -> dict:
    return {"$sum": {"$map": {"input": "$logs", "as": "l", "in": expression}}}


def build_department_summary(month: str, department: Optional[str]) -> dict:
    """Starts from `employees` so people with zero logs count and people who join later do not (R9)."""
    first, last = month_bounds(month)
    match: dict = {"joined_on": {"$lte": last}}
    if department is not None:
        match["department"] = department
    has_work_hours = {"$and": [_in_presence("$$l."), {"$isNumber": "$$l.work_hours"}]}
    pipeline = [
        {"$match": match},
        {"$lookup": {
            "from": "attendance_logs",
            "localField": "emp_code",
            "foreignField": "emp_code",
            "pipeline": [
                {"$match": {"date": {"$gte": first, "$lte": last}}},
                {"$project": {"_id": 0, "date": 1, "status": 1, "half_day": 1, "late_minutes": 1, "work_hours": 1}},
            ],
            "as": "logs",
        }},
        {"$addFields": {
            "present_halves": _over_logs(_present_halves("$$l.", weekdays_only=True)),
            "work_cents": _over_logs({"$cond": [has_work_hours, {"$round": [{"$multiply": ["$$l.work_hours", 100]}, 0]}, 0]}),
            "work_count": _over_logs({"$cond": [has_work_hours, 1, 0]}),
            "late_count": _over_logs({"$cond": [{"$gt": [_late_of("$$l."), 0]}, 1, 0]}),
            "total_late": _over_logs(_late_of("$$l.")),
            "leave_count": _over_logs({"$cond": [{"$eq": ["$$l.status", "LEAVE"]}, 1, 0]}),
            "on_duty_count": _over_logs({"$cond": [{"$eq": ["$$l.status", "ON_DUTY"]}, 1, 0]}),
        }},
        {"$group": {
            "_id": "$department",
            "headcount": {"$sum": 1},
            "present_halves": {"$sum": "$present_halves"},
            "work_cents": {"$sum": "$work_cents"},
            "work_count": {"$sum": "$work_count"},
            "late_count": {"$sum": "$late_count"},
            "total_late": {"$sum": "$total_late"},
            "leave_count": {"$sum": "$leave_count"},
            "on_duty_count": {"$sum": "$on_duty_count"},
        }},
        {"$sort": {"_id": 1}},
    ]
    hint = IDX_EMP_DEPT_JOINED if department is not None else IDX_EMP_JOINED
    return {"collection": "employees", "kind": "aggregate", "pipeline": pipeline, "hint": hint}


def build_late_leaderboard(month: str, limit: int, department: Optional[str]) -> dict:
    first, last = month_bounds(month)
    pipeline: list = [
        {"$match": {"date": {"$gte": first, "$lte": last}}},
        {"$group": {
            "_id": "$emp_code",
            "total": {"$sum": _late_of("$")},
            "late_count": {"$sum": {"$cond": [{"$gt": [_late_of("$"), 0]}, 1, 0]}},
        }},
        {"$match": {"total": {"$gt": 0}}},
        # inner join: logs whose employee does not exist are dropped here
        {"$lookup": {"from": "employees", "localField": "_id", "foreignField": "emp_code", "as": "emp"}},
        {"$unwind": "$emp"},
    ]
    if department is not None:
        pipeline.append({"$match": {"emp.department": department}})  # ranking happens inside the department
    pipeline += [
        # standard competition ranking (1, 2, 2, 4): ties share a rank and the next rank is skipped
        {"$setWindowFields": {"sortBy": {"total": -1}, "output": {"rank": {"$rank": {}}}}},
        {"$match": {"rank": {"$lte": limit}}},  # limit applies AFTER ranking
        {"$sort": {"total": -1, "_id": 1}},
        {"$project": {
            "_id": 0, "rank": 1, "emp_code": "$_id", "name": "$emp.name", "department": "$emp.department",
            "total": 1, "late_count": 1,
        }},
    ]
    return {"collection": "attendance_logs", "kind": "aggregate", "pipeline": pipeline, "hint": IDX_LOG_DATE}


def build_department_trend(department: str, d_from: str, d_to: str) -> dict:
    start, end = date.fromisoformat(d_from), date.fromisoformat(d_to)
    if end < start:
        unprocessable(("query", "to"), "to must not be before from")
    if (end - start).days + 1 > MAX_TREND_DAYS:
        unprocessable(("query", "to"), f"the range must not exceed {MAX_TREND_DAYS} days")
    roster = list(db.employees.find({"department": department}, {"_id": 0, "emp_code": 1, "joined_on": 1}))
    if not roster:
        raise HTTPException(404, "unknown department")
    codes = [r["emp_code"] for r in roster]
    joined = sorted(r["joined_on"] for r in roster)
    lower = datetime(start.year, start.month, start.day, tzinfo=UTC)
    last_day = datetime(end.year, end.month, end.day, tzinfo=UTC)
    upper = last_day + timedelta(days=1)
    pipeline = [
        {"$match": {"emp_code": {"$in": codes}, "date": {"$gte": d_from, "$lte": d_to}}},
        {"$group": {
            "_id": "$date",
            "halves": {"$sum": _present_halves("$", weekdays_only=False)},  # present_count counts weekends too
            "late_count": {"$sum": {"$cond": [{"$gt": [_late_of("$"), 0]}, 1, 0]}},
        }},
        {"$addFields": {"d": {"$dateFromString": {"dateString": "$_id", "format": "%Y-%m-%d"}}}},
        # one row per calendar day, filled in by the database; the extra $match makes the upper bound safe
        {"$densify": {"field": "d", "range": {"step": 1, "unit": "day", "bounds": [lower, upper]}}},
        {"$match": {"d": {"$lte": last_day}}},
        {"$addFields": {
            "ds": {"$dateToString": {"date": "$d", "format": "%Y-%m-%d"}},
            "dow": {"$dayOfWeek": "$d"},
            "halves": {"$ifNull": ["$halves", 0]},
            "late_count": {"$ifNull": ["$late_count", 0]},
        }},
        {"$addFields": {
            "headcount": {"$size": {"$filter": {"input": {"$literal": joined}, "as": "j", "cond": {"$lte": ["$$j", "$ds"]}}}},
            "is_wd": {"$and": [{"$gte": ["$dow", 2]}, {"$lte": ["$dow", 6]}]},
        }},
        # attendance_rate in units of 1e-4, half-up: floor((10000*halves + H) / (2H))
        {"$addFields": {"rate": {"$cond": [
            {"$and": ["$is_wd", {"$gt": ["$headcount", 0]}]},
            {"$floor": {"$divide": [{"$add": [{"$multiply": ["$halves", 10000]}, "$headcount"]}, {"$multiply": ["$headcount", 2]}]}},
            None,
        ]}}},
        {"$sort": {"d": 1}},
        # 7-row window inside the requested range; nulls are skipped by $sum and not counted
        {"$setWindowFields": {"sortBy": {"d": 1}, "output": {
            "win_sum": {"$sum": "$rate", "window": {"documents": [-6, 0]}},
            "win_cnt": {"$sum": {"$cond": [{"$ne": ["$rate", None]}, 1, 0]}, "window": {"documents": [-6, 0]}},
        }}},
        {"$project": {
            "_id": 0, "date": "$ds", "is_wd": 1, "headcount": 1, "halves": 1, "late_count": 1,
            "rate": 1, "win_sum": 1, "win_cnt": 1,
        }},
        {"$sort": {"date": 1}},
    ]
    return {"collection": "attendance_logs", "kind": "aggregate", "pipeline": pipeline, "hint": IDX_LOG_UNIQUE}


def run_aggregate(spec: dict) -> list:
    return list(db[spec["collection"]].aggregate(spec["pipeline"], hint=spec.get("hint")))


def explain_spec(spec: dict) -> dict:
    """MongoDB explain (executionStats) of exactly the operation the endpoint runs."""
    if spec["kind"] == "find":
        inner = {
            "find": spec["collection"], "filter": spec["filter"], "projection": spec["projection"],
            "sort": spec["sort"], "skip": spec["skip"], "limit": spec["limit"],
        }
    else:
        inner = {"aggregate": spec["collection"], "pipeline": spec["pipeline"], "cursor": {}}
        if spec.get("hint"):
            inner["hint"] = spec["hint"]
    result = db.command({"explain": inner, "verbosity": "executionStats"})
    return json.loads(json_util.dumps(result, json_options=json_util.RELAXED_JSON_OPTIONS))


# --------------------------------------------------------------------------- #
# Endpoints: system and employees
# --------------------------------------------------------------------------- #
@app.get("/health")
def health():
    try:
        client.admin.command("ping")
    except PyMongoError:
        raise HTTPException(503, "database unavailable")
    if not _state["indexes_ready"]:
        _state["indexes_ready"] = ensure_indexes()
    return {"status": "ok"}


@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn):
    doc = body.model_dump()
    doc["created_at"] = utc_now_ms()
    try:
        db.employees.insert_one(doc)  # the unique emp_code index is the guard, no check-then-insert
    except DuplicateKeyError:
        raise HTTPException(409, "emp_code already exists")
    return ser_employee(doc)


@app.get("/employees")
def list_employees(department: Optional[str] = None, page: Page = 1, page_size: PageSize = 20):
    query = {} if department is None else {"department": department}
    total = db.employees.count_documents(query)
    skip = _skip_for(page, page_size)
    items = []
    if skip <= MAX_SKIP:
        cursor = db.employees.find(query, {"_id": 0}).sort("emp_code", 1).skip(skip).limit(page_size)
        items = [ser_employee(doc) for doc in cursor]
    return {"items": items, "total": total, "page": page, "page_size": page_size}


# --------------------------------------------------------------------------- #
# Endpoints: attendance
# --------------------------------------------------------------------------- #
@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInIn):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if emp is None:
        raise HTTPException(404, "unknown employee")
    ts = ms_to_dt(body.punched_at) if body.punched_at is not None else utc_now()
    day = attendance_date(ts, emp["shift_start"], emp["shift_end"])
    doc = {
        "emp_code": body.emp_code,
        "date": day,
        "status": body.status,
        "punch_in": ts,
        "punch_out": None,
        **derive(emp, day, body.status, ts, None),
        "history": [],
    }
    try:
        db.attendance_logs.insert_one(doc)  # the unique (emp_code, date) index decides races
    except DuplicateKeyError:
        raise HTTPException(409, "already punched in for this date")
    return ser_log(doc)


@app.post("/attendance/punch-out")
def punch_out(body: PunchOutIn):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if emp is None:
        raise HTTPException(404, "unknown employee")
    ts = ms_to_dt(body.punched_at) if body.punched_at is not None else utc_now()
    # most recent record whose punch_in is at or before punched_at (works across midnight)
    rec = db.attendance_logs.find_one(
        {"emp_code": body.emp_code, "punch_in": {"$lte": ts}}, sort=[("punch_in", -1)]
    )
    if rec is None:
        raise HTTPException(404, "no punch-in found")
    if rec.get("punch_out") is not None:
        raise HTTPException(409, "already punched out")
    punch_in_at = aware(rec["punch_in"])
    if ts <= punch_in_at:
        unprocessable(("body", "punched_at"), "punched_at must be after the punch-in")
    if ts - punch_in_at > timedelta(hours=24):
        unprocessable(("body", "punched_at"), "punched_at is more than 24 hours after the punch-in")
    derived = derive(emp, rec["date"], rec["status"], punch_in_at, ts)
    updated = db.attendance_logs.find_one_and_update(
        # only an open record of the same punch-in can be closed: two simultaneous punch-outs give one 200
        {"emp_code": body.emp_code, "date": rec["date"], "punch_in": rec["punch_in"], "punch_out": None},
        {"$set": {
            "punch_out": ts,
            "work_hours": derived["work_hours"],
            "overtime_minutes": derived["overtime_minutes"],
            "half_day": derived["half_day"],
        }},
        return_document=ReturnDocument.AFTER,
    )
    if updated is None:
        raise HTTPException(409, "already punched out")
    return ser_log(updated)


@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[DateStr] = None,
    date_to: Optional[DateStr] = None,
    status: Optional[Status] = None,
    page: Page = 1,
    page_size: PageSize = 20,
):
    spec = build_attendance_list(emp_code, date_from, date_to, status, page, page_size)
    total = db.attendance_logs.count_documents(spec["filter"])
    items = []
    if spec["skip"] <= MAX_SKIP:
        cursor = (
            db.attendance_logs.find(spec["filter"], spec["projection"])
            .sort(list(spec["sort"].items()))
            .skip(spec["skip"])
            .limit(spec["limit"])
        )
        items = [ser_log(doc) for doc in cursor]
    return {"items": items, "total": total, "page": page, "page_size": page_size}


_HISTORY_FIELDS = ("status", "punch_in", "punch_out", "work_hours", "late_minutes", "overtime_minutes", "half_day")


def _comparable(field: str, value: Any) -> Any:
    return dt_to_ms(value) if field in ("punch_in", "punch_out") else value


@app.patch("/attendance/{emp_code}/{date}")
def regularize(
    emp_code: str,
    day: Annotated[str, AfterValidator(_valid_date), Path(alias="date")],
    body: RegularizeIn,
):
    emp = db.employees.find_one({"emp_code": emp_code})
    if emp is None:
        raise HTTPException(404, "unknown employee")
    rec = db.attendance_logs.find_one({"emp_code": emp_code, "date": day})
    if rec is None:
        raise HTTPException(404, "no attendance record for this date")

    old_status = rec["status"]
    cur_in, cur_out = aware(rec.get("punch_in")), aware(rec.get("punch_out"))
    in_given, out_given = body.punch_in is not None, body.punch_out is not None

    status = body.status or old_status
    if body.status is None and old_status not in PRESENCE and in_given:
        status = "PRESENT"  # F10: a punch-in on an ABSENT/LEAVE record means the person did work

    if status in PRESENCE:
        punch_in_at = ms_to_dt(body.punch_in) if in_given else cur_in
        punch_out_at = ms_to_dt(body.punch_out) if out_given else cur_out
        if punch_in_at is None:
            unprocessable(("body", "punch_in"), "a presence status requires a punch_in")
        if in_given and attendance_date(punch_in_at, emp["shift_start"], emp["shift_end"]) != day:
            unprocessable(("body", "punch_in"), "punch_in must stay on the record's attendance date")
        if punch_out_at is not None:
            if punch_out_at <= punch_in_at:
                unprocessable(("body", "punch_out"), "punch_out must be after punch_in")
            if punch_out_at - punch_in_at > timedelta(hours=24):
                unprocessable(("body", "punch_out"), "punch_out must be within 24 hours of punch_in")
    else:  # ABSENT or LEAVE clears both punch times
        if in_given or out_given:
            unprocessable(("body", "punch_in" if in_given else "punch_out"),
                          "ABSENT and LEAVE records cannot carry punch times")
        punch_in_at = punch_out_at = None

    new_values = {"status": status, "punch_in": punch_in_at, "punch_out": punch_out_at,
                  **derive(emp, day, status, punch_in_at, punch_out_at)}
    old_values = {
        "status": old_status,
        "punch_in": cur_in,
        "punch_out": cur_out,
        "work_hours": rec.get("work_hours"),
        "late_minutes": rec.get("late_minutes") or 0,
        "overtime_minutes": rec.get("overtime_minutes") or 0,
        "half_day": bool(rec.get("half_day", False)),
    }
    changes = {
        field: {"from": old_values[field], "to": new_values[field]}
        for field in _HISTORY_FIELDS
        if _comparable(field, old_values[field]) != _comparable(field, new_values[field])
    }
    # "changes nothing" is judged on the three input fields; derived diffs are still recorded when inputs change
    if not any(field in changes for field in ("status", "punch_in", "punch_out")):
        unprocessable(("body",), "the request changes nothing")

    entry = {"at": utc_now_ms(), "by": body.regularized_by, "reason": body.reason, "changes": changes}
    guard = {
        "emp_code": emp_code,
        "date": day,
        "status": old_status,
        "punch_in": rec.get("punch_in"),
        "punch_out": rec.get("punch_out"),
    }
    # optimistic concurrency: the update only applies if nobody changed the record since we read it,
    # so two simultaneous corrections cannot silently overwrite each other's history entry
    if "history" in rec and rec["history"] is not None:
        guard["history"] = {"$size": len(rec["history"])}
    else:
        guard["history"] = {"$exists": False}
    updated = db.attendance_logs.find_one_and_update(
        guard,
        {"$set": new_values, "$push": {"history": entry}},
        return_document=ReturnDocument.AFTER,
    )
    if updated is None:
        raise HTTPException(409, "the record was changed by someone else, please retry")
    return ser_log(updated)


# --------------------------------------------------------------------------- #
# Endpoints: analytics
# --------------------------------------------------------------------------- #
@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(emp_code: str, month: MonthStr):
    emp = db.employees.find_one({"emp_code": emp_code}, {"_id": 0, "joined_on": 1})
    if emp is None:
        raise HTTPException(404, "unknown employee")
    rows = run_aggregate(build_employee_monthly(emp_code, month))
    group = rows[0] if rows else {}
    first, last = month_bounds(month)
    start = max(date.fromisoformat(emp["joined_on"]), date.fromisoformat(first))
    working_days = count_weekdays(start, date.fromisoformat(last))
    halves = int(group.get("present_halves", 0))
    pct = None if working_days == 0 else div_half_up(halves * 5000, working_days) / 100
    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": working_days,
        "present_days": halves / 2,
        "leave_days": int(group.get("leave_days", 0)),
        "late_count": int(group.get("late_count", 0)),
        "total_late_minutes": int(group.get("total_late_minutes", 0)),
        "total_overtime_minutes": int(group.get("total_overtime_minutes", 0)),
        "attendance_pct": pct,
    }


@app.get("/analytics/departments/summary")
def department_summary(month: MonthStr, department: Optional[str] = None):
    rows = run_aggregate(build_department_summary(month, department))
    items = []
    for row in rows:
        count = int(row["work_count"])
        avg = None if count == 0 else div_half_up(int(row["work_cents"]), count) / 100  # mean over records
        items.append({
            "department": row["_id"],
            "headcount": int(row["headcount"]),
            "present_days": int(row["present_halves"]) / 2,
            "avg_work_hours": avg,
            "late_count": int(row["late_count"]),
            "total_late_minutes": int(row["total_late"]),
            "leave_count": int(row["leave_count"]),
            "on_duty_count": int(row["on_duty_count"]),
        })
    return {"month": month, "items": items}


@app.get("/analytics/leaderboard/late")
def late_leaderboard(
    month: MonthStr,
    limit: Annotated[int, Query(ge=1, le=50)] = 10,
    department: Optional[str] = None,
):
    rows = run_aggregate(build_late_leaderboard(month, limit, department))
    items = [
        {
            "rank": int(row["rank"]),
            "emp_code": row["emp_code"],
            "name": row["name"],
            "department": row["department"],
            "total_late_minutes": int(row["total"]),
            "late_count": int(row["late_count"]),
        }
        for row in rows
    ]
    return {"month": month, "items": items}


@app.get("/analytics/departments/{department}/trend")
def department_trend(
    department: str,
    from_date: Annotated[DateStr, Query(alias="from")],
    to_date: Annotated[DateStr, Query(alias="to")],
):
    rows = run_aggregate(build_department_trend(department, from_date, to_date))
    items = []
    for row in rows:
        rate = None if row["rate"] is None else int(row["rate"])
        window_count = int(row["win_cnt"])
        moving = None if window_count == 0 else div_half_up(int(row["win_sum"]), window_count)
        items.append({
            "date": row["date"],
            "is_working_day": bool(row["is_wd"]),
            "headcount": int(row["headcount"]),
            "present_count": int(row["halves"]) / 2,
            "late_count": int(row["late_count"]),
            "attendance_rate": None if rate is None else rate / 10000,
            "moving_avg_7d": None if moving is None else moving / 10000,
        })
    return {"department": department, "items": items}


# --------------------------------------------------------------------------- #
# Endpoint: explain
# --------------------------------------------------------------------------- #
def _required(name: str, value: Any) -> Any:
    if value is None:
        unprocessable(("query", name), "Field required")
    return value


@app.get("/admin/explain/{endpoint}")
def explain_endpoint(
    endpoint: Literal["attendance_list", "employee_monthly", "department_summary", "late_leaderboard", "department_trend"],
    emp_code: Optional[str] = None,
    month: Optional[MonthStr] = None,
    department: Optional[str] = None,
    limit: Annotated[int, Query(ge=1, le=50)] = 10,
    date_from: Optional[DateStr] = None,
    date_to: Optional[DateStr] = None,
    status: Optional[Status] = None,
    from_date: Annotated[Optional[DateStr], Query(alias="from")] = None,
    to_date: Annotated[Optional[DateStr], Query(alias="to")] = None,
    page: Page = 1,
    page_size: PageSize = 20,
):
    if endpoint == "attendance_list":
        spec = build_attendance_list(emp_code, date_from, date_to, status, page, page_size)
    elif endpoint == "employee_monthly":
        spec = build_employee_monthly(_required("emp_code", emp_code), _required("month", month))
    elif endpoint == "department_summary":
        spec = build_department_summary(_required("month", month), department)
    elif endpoint == "late_leaderboard":
        spec = build_late_leaderboard(_required("month", month), limit, department)
    else:
        spec = build_department_trend(
            _required("department", department), _required("from", from_date), _required("to", to_date)
        )
    return {"endpoint": endpoint, "collection": spec["collection"], "explain": explain_spec(spec)}
