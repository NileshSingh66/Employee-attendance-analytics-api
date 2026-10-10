
"""
Employee Attendance & Analytics API
Run: uvicorn app.main:app --port 8000

Configuration:
    MONGO_URI
    MONGO_DB

All API instants use epoch milliseconds.
MongoDB stores instants as UTC BSON datetimes.
"""

import os
import re
import calendar
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Optional, Literal

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query, Path
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from pymongo import MongoClient, ASCENDING, DESCENDING, ReturnDocument
from pymongo.errors import PyMongoError, DuplicateKeyError
from pymongo.collection import Collection

load_dotenv()

client = MongoClient(
    os.getenv("MONGO_URI", "mongodb://127.0.0.1:27017"),
    serverSelectionTimeoutMS=3000,
    tz_aware=True,
    tzinfo=timezone.utc,
)
db = client[os.getenv("MONGO_DB", "attendance_db")]

IST = timezone(timedelta(hours=5, minutes=30))
UTC = timezone.utc
PRESENCE = {"PRESENT", "WFH", "ON_DUTY"}
ALL_STATUSES = PRESENCE | {"ABSENT", "LEAVE"}

app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0")


# ---------------------------------------------------------------------------
# Startup indexes
# ---------------------------------------------------------------------------


@app.on_event("startup")
def create_indexes():
    # Employee indexes

    # Unique index: prevents duplicate employee codes.
    db.employees.create_index(
        [("emp_code", ASCENDING)],
        unique=True,
        name="uq_employee_code",
    )

    # Supports filtering employees by department and employee code.
    db.employees.create_index(
        [("department", ASCENDING), ("emp_code", ASCENDING)],
        name="ix_employee_department_code",
    )

    # Supports filtering by department and joined date.
    db.employees.create_index(
        [("department", ASCENDING), ("joined_on", ASCENDING)],
        name="ix_employee_department_joined",
    )

    # Supports queries filtering only by joined date.
    db.employees.create_index(
        [("joined_on", ASCENDING)],
        name="ix_employee_joined_on",
    )

    # Attendance indexes

    # Unique index: one attendance record per employee per date.
    db.attendance_logs.create_index(
        [("emp_code", ASCENDING), ("date", ASCENDING)],
        unique=True,
        name="uq_attendance_employee_date",
    )

    # Supports attendance queries sorted by date and employee code.
    db.attendance_logs.create_index(
        [("date", DESCENDING), ("emp_code", ASCENDING)],
        name="ix_attendance_date_employee",
    )

    # Supports filtering attendance by status and date.
    db.attendance_logs.create_index(
        [("status", ASCENDING), ("date", DESCENDING)],
        name="ix_attendance_status_date",
    )

    # Supports finding the latest attendance record for an employee.
    db.attendance_logs.create_index(
        [("emp_code", ASCENDING), ("punch_in", DESCENDING)],
        name="ix_attendance_employee_punchin",
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def fail(status_code: int, message: str):
    raise HTTPException(status_code=status_code, detail=message)


def half_up(value, places=2):
    quantum = Decimal("1").scaleb(-places)
    return float(Decimal(str(value)).quantize(quantum, rounding=ROUND_HALF_UP))


def epoch_ms_to_datetime(value: int) -> datetime:
    # Reject seconds, strings, floats, booleans and unreasonable dates.
    if type(value) is not int or not 100_000_000_000 <= value <= 4_102_444_800_000:
        fail(422, "Timestamp must be epoch milliseconds in the supported range")
    try:
        return datetime.fromtimestamp(value / 1000, tz=UTC).replace(microsecond=0)
    except (ValueError, OverflowError, OSError):
        fail(422, "Invalid timestamp")


def datetime_to_ms(value: Optional[datetime]):
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return int(value.timestamp() * 1000)


def utc_datetime(value: datetime) -> datetime:
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).replace(microsecond=0)


def as_ist(value: datetime) -> datetime:
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(IST)


def parse_date(value: str) -> date:
    try:
        result = date.fromisoformat(value)
        if result.isoformat() != value:
            raise ValueError()
        return result
    except (ValueError, TypeError):
        fail(422, "Date must be YYYY-MM-DD")


def parse_month(value: str) -> tuple[date, date]:
    if not re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", value or ""):
        fail(422, "Month must be YYYY-MM")
    year, month = map(int, value.split("-"))
    first = date(year, month, 1)
    last = date(year, month, calendar.monthrange(year, month)[1])
    return first, last


def days_inclusive(start: date, end: date):
    current = start
    while current <= end:
        yield current
        current += timedelta(days=1)


def working_days(start: date, end: date) -> int:
    return sum(1 for day in days_inclusive(start, end) if day.weekday() < 5)


def attendance_date(punch_in: datetime, shift_start: str, shift_end: str) -> str:
    local = as_ist(punch_in)
    start_h, start_m = map(int, shift_start.split(":"))
    end_h, end_m = map(int, shift_end.split(":"))
    start_minutes = start_h * 60 + start_m
    end_minutes = end_h * 60 + end_m
    local_minutes = local.hour * 60 + local.minute

    # For an overnight shift, early-morning punches belong to the prior day.
    if end_minutes <= start_minutes and local_minutes < end_minutes:
        local -= timedelta(days=1)
    return local.date().isoformat()


def shift_start_datetime(day: str, shift_start: str) -> datetime:
    h, m = map(int, shift_start.split(":"))
    local = datetime.combine(parse_date(day), time(h, m), tzinfo=IST)
    return local.astimezone(UTC)


def shift_end_datetime(day: str, shift_start: str, shift_end: str) -> datetime:
    start = parse_date(day)
    sh, sm = map(int, shift_start.split(":"))
    eh, em = map(int, shift_end.split(":"))
    end_day = start + timedelta(days=1) if (eh, em) <= (sh, sm) else start
    return datetime.combine(end_day, time(eh, em), tzinfo=IST).astimezone(UTC)



def compute_late_minutes(punch_in: datetime, shift_start: str) -> int:
    local = as_ist(punch_in)

    h, m = map(int, shift_start.split(":"))
    start_minutes = h * 60 + m

    punch_minutes = local.hour * 60 + local.minute

    delta_seconds = (punch_minutes - start_minutes) * 60 + local.second

    
    if delta_seconds < -(12 * 60 * 60):
        delta_seconds += 24 * 60 * 60

    # Allow a 10-minute grace period.
    if delta_seconds <= 600:
        return 0

    return delta_seconds // 60


def compute_work_hours(punch_in: datetime, punch_out: datetime) -> float:
    seconds = int((punch_out - punch_in).total_seconds())
    return half_up(seconds / 3600, 2)


def compute_overtime(punch_out: datetime, shift_end: str, date_str: str,
                     shift_start: str = "09:30") -> int:
    end = shift_end_datetime(date_str, shift_start, shift_end)
    seconds = int((punch_out - end).total_seconds())
    minutes = seconds // 60
    return minutes if minutes >= 30 else 0


def serialize_employee(doc: dict) -> dict:
    doc = {k: v for k, v in doc.items() if k != "_id"}
    if "created_at" in doc:
        doc["created_at"] = datetime_to_ms(doc["created_at"])
    return doc


def serialize_attendance(doc: dict) -> dict:
    doc = {k: v for k, v in doc.items() if k != "_id"}
    for key in ("punch_in", "punch_out"):
        doc[key] = datetime_to_ms(doc.get(key))
    doc["history"] = doc.get("history", [])
    doc["half_day"] = doc.get("half_day", False)
    for entry in doc["history"]:
        if entry.get("at") is not None:
            entry["at"] = datetime_to_ms(entry["at"])
        for change in entry.get("changes", {}).values():
            for field in ("from", "to"):
                if isinstance(change.get(field), datetime):
                    change[field] = datetime_to_ms(change[field])
    return doc


def require_employee(emp_code: str):
    emp = db.employees.find_one({"emp_code": emp_code})
    if emp is None:
        fail(404, "Employee not found")
    return emp


def validate_page(page: int, page_size: int):
    if page < 1:
        fail(422, "page must be at least 1")
    if page_size < 1 or page_size > 100:
        fail(422, "page_size must be between 1 and 100")


def mongo_explain(collection: Collection, pipeline: list):
    # MongoDB's aggregate explain command with executionStats verbosity.
    return db.command({
        "explain": {
            "aggregate": collection.name,
            "pipeline": pipeline,
            "cursor": {},
        },
        "verbosity": "executionStats",
    })


def status_present_expression():
    return {"$in": ["$status", list(PRESENCE)]}


def add_attendance_filters(emp_code=None, date_from=None, date_to=None, status=None):
    q = {}
    if emp_code:
        q["emp_code"] = emp_code
    if date_from or date_to:
        q["date"] = {}
        if date_from:
            q["date"]["$gte"] = date_from
        if date_to:
            q["date"]["$lte"] = date_to
    if status:
        q["status"] = status
    return q


# ---------------------------------------------------------------------------
# Models and validation
# ---------------------------------------------------------------------------

class EmployeeIn(BaseModel):
    model_config = ConfigDict(extra="ignore")

    emp_code: str = Field(pattern=r"^EMP\d{4,6}$")
    name: str = Field(min_length=1, max_length=100)
    email: str = Field(min_length=3, max_length=120)
    department: str = Field(min_length=1, max_length=50)
    shift_start: str = Field(default="09:30", pattern=r"^(?:[01]\d|2[0-3]):[0-5]\d$")
    shift_end: str = Field(default="18:30", pattern=r"^(?:[01]\d|2[0-3]):[0-5]\d$")
    joined_on: str

    @field_validator("email")
    @classmethod
    def valid_email(cls, value):
        if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", value):
            raise ValueError("Invalid email address")
        return value

    @field_validator("joined_on")
    @classmethod
    def valid_joined_on(cls, value):
        parse_date(value)
        return value

    @model_validator(mode="after")
    def validate_shift(self):
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start and shift_end must differ")
        return self


class PunchInIn(BaseModel):
    model_config = ConfigDict(extra="ignore")

    emp_code: str
    punched_at: Optional[int] = None
    status: Literal["PRESENT", "WFH", "ON_DUTY"] = "PRESENT"

    @field_validator("punched_at")
    @classmethod
    def valid_timestamp(cls, value):
        if value is not None:
            epoch_ms_to_datetime(value)
        return value


class PunchOutIn(BaseModel):
    model_config = ConfigDict(extra="ignore")

    emp_code: str
    punched_at: Optional[int] = None

    @field_validator("punched_at")
    @classmethod
    def valid_timestamp(cls, value):
        if value is not None:
            epoch_ms_to_datetime(value)
        return value


class RegularizeIn(BaseModel):
    model_config = ConfigDict(extra="ignore")

    reason: str = Field(min_length=5, max_length=200)
    regularized_by: str = Field(min_length=1, max_length=50)
    status: Optional[Literal["PRESENT", "WFH", "ON_DUTY", "ABSENT", "LEAVE"]] = None
    punch_in: Optional[int] = None
    punch_out: Optional[int] = None

    @field_validator("punch_in", "punch_out")
    @classmethod
    def valid_timestamp(cls, value):
        if value is not None:
            epoch_ms_to_datetime(value)
        return value


# ---------------------------------------------------------------------------
# Health and employee endpoints
# ---------------------------------------------------------------------------

@app.get("/health")
def health():
    try:
        db.command("ping")
        return {"status": "ok"}
    except PyMongoError:
        return JSONResponse(
            status_code=503,
            content={"status": "error", "detail": "MongoDB unavailable"},
        )


@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn):
    doc = body.model_dump()
    doc["created_at"] = datetime.now(UTC).replace(microsecond=0)
    try:
        db.employees.insert_one(doc)
    except DuplicateKeyError:
        fail(409, "emp_code already exists")
    return serialize_employee(doc)


@app.get("/employees")
def list_employees(
    department: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    q = {"department": department} if department is not None else {}
    total = db.employees.count_documents(q)
    cursor = (
        db.employees.find(q, {"_id": 0})
        .sort("emp_code", ASCENDING)
        .skip((page - 1) * page_size)
        .limit(page_size)
    )
    return {
        "items": [serialize_employee(doc) for doc in cursor],
        "total": total,
        "page": page,
        "page_size": page_size,
    }


# ---------------------------------------------------------------------------
# Attendance punch-in, punch-out and list
# ---------------------------------------------------------------------------

@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInIn):
    emp = require_employee(body.emp_code)
    ts = epoch_ms_to_datetime(body.punched_at) if body.punched_at is not None else datetime.now(UTC).replace(microsecond=0)
    d = attendance_date(ts, emp["shift_start"], emp["shift_end"])

    doc = {
        "emp_code": body.emp_code,
        "date": d,
        "status": body.status,
        "punch_in": ts,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": compute_late_minutes(ts, emp["shift_start"]),
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }
    try:
        db.attendance_logs.insert_one(doc)
    except DuplicateKeyError:
        fail(409, "Already punched in for this attendance date")
    return serialize_attendance(doc)


@app.post("/attendance/punch-out")
def punch_out(body: PunchOutIn):
    emp = require_employee(body.emp_code)
    ts = epoch_ms_to_datetime(body.punched_at) if body.punched_at is not None else datetime.now(UTC).replace(microsecond=0)

    # Find the latest open shift whose punch-in is not after the requested punch-out.
    record = db.attendance_logs.find_one(
        {
            "emp_code": body.emp_code,
            "punch_in": {"$ne": None, "$lte": ts},
            "punch_out": None,
            "status": {"$in": list(PRESENCE)},
        },
        sort=[("punch_in", DESCENDING)],
    )
    if record is None:
        fail(404, "No open punch-in found")

    start = utc_datetime(record["punch_in"])
    ts = utc_datetime(ts)
    elapsed = (ts - start).total_seconds()
    if elapsed <= 0 or elapsed > 24 * 3600:
        fail(422, "Punch-out must be after punch-in and within 24 hours")

    hours = compute_work_hours(start, ts)
    overtime = compute_overtime(
        ts, emp["shift_end"], record["date"], emp["shift_start"]
    )
    half_day = hours < 4.50

    # Atomic condition: only one concurrent punch-out can update the open record.
    updated = db.attendance_logs.find_one_and_update(
        {"_id": record["_id"], "punch_out": None},
        {"$set": {
            "punch_out": ts,
            "work_hours": hours,
            "overtime_minutes": overtime,
            "half_day": half_day,
        }},
        return_document=ReturnDocument.AFTER,
    )
    if updated is None:
        fail(409, "Attendance record has already been punched out")
    return serialize_attendance(updated)


@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    if date_from:
        parse_date(date_from)
    if date_to:
        parse_date(date_to)
    if date_from and date_to and date_from > date_to:
        fail(422, "date_from must be on or before date_to")
    if status and status not in ALL_STATUSES:
        fail(422, "Invalid status")

    q = add_attendance_filters(emp_code, date_from, date_to, status)
    total = db.attendance_logs.count_documents(q)
    cursor = (
        db.attendance_logs.find(q, {"_id": 0})
        .sort([("date", DESCENDING), ("emp_code", ASCENDING)])
        .skip((page - 1) * page_size)
        .limit(page_size)
    )
    return {
        "items": [serialize_attendance(doc) for doc in cursor],
        "total": total,
        "page": page,
        "page_size": page_size,
    }


# ---------------------------------------------------------------------------
# Manual attendance regularization
# ---------------------------------------------------------------------------

@app.patch("/attendance/{emp_code}/{date}")
def regularize_attendance(
    emp_code: str,
    date: str = Path(..., pattern=r"^\d{4}-\d{2}-\d{2}$"),
    body: RegularizeIn = ...,
):
    parse_date(date)
    emp = require_employee(emp_code)
    record = db.attendance_logs.find_one({"emp_code": emp_code, "date": date})
    if record is None:
        fail(404, "Attendance record not found")

    final_status = body.status if body.status is not None else record["status"]
    old_in = record.get("punch_in")
    old_out = record.get("punch_out")
    new_in = epoch_ms_to_datetime(body.punch_in) if body.punch_in is not None else old_in
    new_out = epoch_ms_to_datetime(body.punch_out) if body.punch_out is not None else old_out

    if final_status in {"ABSENT", "LEAVE"}:
        if body.punch_in is not None or body.punch_out is not None:
            fail(422, "ABSENT and LEAVE cannot have punch times")
        new_in, new_out = None, None
    elif new_in is None:
        fail(422, "Presence statuses require punch_in")

    if new_in is not None:
        new_in = utc_datetime(new_in)
        if attendance_date(new_in, emp["shift_start"], emp["shift_end"]) != date:
            fail(422, "punch_in does not belong to the attendance date")
    if new_out is not None:
        new_out = utc_datetime(new_out)
        if new_in is None or new_out <= new_in:
            fail(422, "punch_out must be after punch_in")
        if (new_out - new_in).total_seconds() > 24 * 3600:
            fail(422, "punch_out must be within 24 hours of punch_in")
    if final_status in PRESENCE and new_out is None:
        # Open attendance is allowed for regularization.
        pass

    new_hours = compute_work_hours(new_in, new_out) if new_in and new_out else None
    new_late = compute_late_minutes(new_in, emp["shift_start"]) if new_in else 0
    new_ot = compute_overtime(new_out, emp["shift_end"], date, emp["shift_start"]) if new_out else 0
    new_half = bool(new_hours is not None and new_hours < 4.50)

    final_fields = {
        "status": final_status,
        "punch_in": new_in,
        "punch_out": new_out,
        "work_hours": new_hours,
        "late_minutes": new_late,
        "overtime_minutes": new_ot,
        "half_day": new_half,
    }

    changes = {}
    for key, new_value in final_fields.items():
        old_value = record.get(key, False if key == "half_day" else None)
        if key == "half_day":
            old_value = record.get(key, False)
        if old_value != new_value:
            changes[key] = {"from": old_value, "to": new_value}
    if not changes:
        fail(422, "Regularization contains no changes")

    entry = {
        "at": datetime.now(UTC).replace(microsecond=0),
        "by": body.regularized_by,
        "reason": body.reason,
        "changes": changes,
    }

    # Update only if the old values are still present. Retry is deliberately not
    # automatic: clients can re-read and retry after a concurrent conflict.
    old_values = {
        "status": record.get("status"),
        "punch_in": old_in,
        "punch_out": old_out,
        "work_hours": record.get("work_hours"),
        "late_minutes": record.get("late_minutes", 0),
        "overtime_minutes": record.get("overtime_minutes", 0),
        "half_day": record.get("half_day", False),
    }
    updated = db.attendance_logs.find_one_and_update(
        {"_id": record["_id"], **old_values},
        {
            "$set": final_fields,
            "$push": {"history": entry},
        },
        return_document=ReturnDocument.AFTER,
    )
    if updated is None:
        fail(409, "Record changed concurrently; reload and retry")
    return serialize_attendance(updated)


# ---------------------------------------------------------------------------
# Employee monthly analytics
# ---------------------------------------------------------------------------

@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(emp_code: str, month: str):
    emp = require_employee(emp_code)
    first, last = parse_month(month)
    start = max(first, parse_date(emp["joined_on"]))
    workdays = working_days(start, last) if start <= last else 0

    pipeline = [
        {"$match": {"emp_code": emp_code, "date": {"$gte": first.isoformat(), "$lte": last.isoformat()}}},
        {"$group": {
            "_id": None,
            "present_days": {"$sum": {"$cond": [
                {"$and": [
                    {"$in": ["$status", list(PRESENCE)]},
                    {"$lte": [{"$dayOfWeek": {"$dateFromString": {"dateString": "$date"}}}, 6]},
                    {"$gte": [{"$dayOfWeek": {"$dateFromString": {"dateString": "$date"}}}, 2]},
                ]},
                {"$cond": [{"$eq": ["$half_day", True]}, 0.5, 1]},
                0,
            ]}},
            "leave_days": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
            "late_count": {"$sum": {"$cond": [{"$gt": ["$late_minutes", 0]}, 1, 0]}},
            "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}},
            "total_overtime_minutes": {"$sum": {"$ifNull": ["$overtime_minutes", 0]}},
        }},
    ]
    result = list(db.attendance_logs.aggregate(pipeline))
    data = result[0] if result else {}
    present_days = data.get("present_days", 0)
    attendance_pct = half_up(present_days / workdays * 100, 4) if workdays else None

    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": workdays,
        "present_days": present_days,
        "leave_days": data.get("leave_days", 0),
        "late_count": data.get("late_count", 0),
        "total_late_minutes": data.get("total_late_minutes", 0),
        "total_overtime_minutes": data.get("total_overtime_minutes", 0),
        "attendance_pct": attendance_pct,
    }


# ---------------------------------------------------------------------------
# Department summary
# ---------------------------------------------------------------------------

@app.get("/analytics/departments/summary")
def department_summary(month: str):
    first, last = parse_month(month)
    first_s = first.isoformat()
    last_s = last.isoformat()

    pipeline = [
        # Include employees who joined on or before month-end.
        {"$match": {
            "joined_on": {"$lte": last_s}
        }},

        # Fetch each employee's attendance records for the selected month.
        {"$lookup": {
            "from": "attendance_logs",
            "let": {"code": "$emp_code"},
            "pipeline": [
                {"$match": {
                    "$expr": {
                        "$and": [
                            {"$eq": ["$emp_code", "$$code"]},
                            {"$gte": ["$date", first_s]},
                            {"$lte": ["$date", last_s]},
                        ]
                    }
                }}
            ],
            "as": "logs",
        }},

        # Keep employees even when they have no attendance records.
        {"$unwind": {
            "path": "$logs",
            "preserveNullAndEmptyArrays": True,
        }},

        # Calculate department-level metrics.
        {"$group": {
            "_id": "$department",

            # Track unique employees for the department headcount.
            "employees": {
                "$addToSet": "$emp_code"
            },

            # Count present weekdays; half-days count as 0.5.
            "present_days": {
                "$sum": {
                    "$cond": [
                        {"$and": [
                            {"$in": ["$logs.status", list(PRESENCE)]},
                            {"$ne": [
                                {"$ifNull": ["$logs.date", None]},
                                None
                            ]},
                            {"$lte": [
                                {"$dayOfWeek": {
                                    "$dateFromString": {
                                        "dateString": "$logs.date"
                                    }
                                }},
                                6
                            ]},
                            {"$gte": [
                                {"$dayOfWeek": {
                                    "$dateFromString": {
                                        "dateString": "$logs.date"
                                    }
                                }},
                                2
                            ]},
                        ]},
                        {
                            "$cond": [
                                {"$eq": ["$logs.half_day", True]},
                                0.5,
                                1
                            ]
                        },
                        0
                    ]
                }
            },

            # Average work hours across eligible attendance records.
            "avg_work_hours": {
                "$avg": {
                    "$cond": [
                        {"$and": [
                            {"$in": ["$logs.status", list(PRESENCE)]},
                            {"$ne": ["$logs.work_hours", None]},
                        ]},
                        "$logs.work_hours",
                        None
                    ]
                }
            },

            "late_count": {
                "$sum": {
                    "$cond": [
                        {"$gt": ["$logs.late_minutes", 0]},
                        1,
                        0
                    ]
                }
            },

            "total_late_minutes": {
                "$sum": {
                    "$ifNull": ["$logs.late_minutes", 0]
                }
            },

            "leave_count": {
                "$sum": {
                    "$cond": [
                        {"$eq": ["$logs.status", "LEAVE"]},
                        1,
                        0
                    ]
                }
            },

            "on_duty_count": {
                "$sum": {
                    "$cond": [
                        {"$eq": ["$logs.status", "ON_DUTY"]},
                        1,
                        0
                    ]
                }
            },
        }},

        # Shape the response and round average work hours to 2 decimals.
        {"$project": {
            "_id": 0,
            "department": "$_id",
            "headcount": {"$size": "$employees"},
            "present_days": 1,

            "avg_work_hours": {
                "$cond": [
                    {"$eq": ["$avg_work_hours", None]},
                    None,
                    {"$round": ["$avg_work_hours", 2]}
                ]
            },

            "late_count": 1,
            "total_late_minutes": 1,
            "leave_count": 1,
            "on_duty_count": 1,
        }},

        # Return departments alphabetically.
        {"$sort": {"department": 1}},
    ]

    items = list(
        db.employees.aggregate(
            pipeline,
            allowDiskUse=True
        )
    )

    # Exclude departments with zero eligible employees.
    items = [
        item for item in items
        if item["headcount"] > 0
    ]

    return {
        "month": month,
        "items": items
    }

# ---------------------------------------------------------------------------
# Late leaderboard with competition ranking
# ---------------------------------------------------------------------------

def build_late_leaderboard_pipeline(
    first,
    last,
    department: Optional[str] = None,
    limit: int = 10,
):
    pipeline = [
        {
            "$match": {
                "date": {
                    "$gte": first.isoformat(),
                    "$lte": last.isoformat(),
                },
                "late_minutes": {"$gt": 0},
            }
        },
        {
            "$group": {
                "_id": "$emp_code",
                "total_late_minutes": {"$sum": "$late_minutes"},
            }
        },
        {
            "$match": {
                "total_late_minutes": {"$gt": 0},
            }
        },
    ]

    if department is not None:
        pipeline.extend([
            {
                "$lookup": {
                    "from": "employees",
                    "localField": "_id",
                    "foreignField": "emp_code",
                    "as": "employee",
                }
            },
            {"$unwind": "$employee"},
            {"$match": {"employee.department": department}},
        ])

    pipeline.extend([
        {
            "$setWindowFields": {
                "sortBy": {"total_late_minutes": -1},
                "output": {
                    "rank": {"$rank": {}},
                },
            }
        },
        # Keep everyone whose rank is within the requested limit.
        # This includes all employees tied at the cutoff rank.
        {
            "$match": {
                "rank": {"$lte": limit},
            }
        },
        {
            "$sort": {
                "total_late_minutes": -1,
                "_id": 1,
            }
        },
        {
            "$project": {
                "_id": 0,
                "emp_code": "$_id",
                "total_late_minutes": 1,
                "rank": 1,
            }
        },
    ])

    return pipeline


@app.get("/analytics/leaderboard/late")
def late_leaderboard(
    month: str,
    department: Optional[str] = None,
    limit: int = Query(10, ge=1, le=1000),
):
    first, last = parse_month(month)

    pipeline = build_late_leaderboard_pipeline(
        first=first,
        last=last,
        department=department,
        limit=limit,
    )

    items = list(
        db.attendance_logs.aggregate(
            pipeline,
            allowDiskUse=True,
        )
    )

    return {
        "month": month,
        "items": items,
    }



# ---------------------------------------------------------------------------
# Department daily trend and seven-day moving average
# ---------------------------------------------------------------------------

def month_bounds(month):
    year, month_number = map(int, month.split("-"))
    first = date(year, month_number, 1)

    if month_number == 12:
        next_month = date(year + 1, 1, 1)
    else:
        next_month = date(year, month_number + 1, 1)

    last = next_month - timedelta(days=1)
    return first, last


def build_department_summary_pipeline(month: str):
    first, last = month_bounds(month)

    return [
        {"$match": {"joined_on": {"$lte": last.isoformat()}}},
        {
            "$lookup": {
                "from": "attendance_logs",
                "let": {"code": "$emp_code"},
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {
                                "$and": [
                                    {"$eq": ["$emp_code", "$$code"]},
                                    {"$gte": ["$date", first.isoformat()]},
                                    {"$lte": ["$date", last.isoformat()]},
                                ]
                            }
                        }
                    }
                ],
                "as": "logs",
            }
        },
        {"$unwind": {"path": "$logs", "preserveNullAndEmptyArrays": True}},
        {
            "$group": {
                "_id": "$department",
                "employees": {"$addToSet": "$emp_code"},
                "present_days": {
                    "$sum": {
                        "$cond": [
                            {"$in": ["$logs.status", list(PRESENCE)]},
                            1,
                            0,
                        ]
                    }
                },
                "late_count": {
                    "$sum": {
                        "$cond": [
                            {"$gt": ["$logs.late_minutes", 0]},
                            1,
                            0,
                        ]
                    }
                },
                "total_late_minutes": {
                    "$sum": {"$ifNull": ["$logs.late_minutes", 0]}
                },
                "leave_count": {
                    "$sum": {
                        "$cond": [
                            {"$eq": ["$logs.status", "LEAVE"]},
                            1,
                            0,
                        ]
                    }
                },
                "on_duty_count": {
                    "$sum": {
                        "$cond": [
                            {"$eq": ["$logs.status", "ON_DUTY"]},
                            1,
                            0,
                        ]
                    }
                },
                "work_hours": {
                    "$push": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$in": ["$logs.status", list(PRESENCE)]},
                                    {"$ne": ["$logs.work_hours", None]},
                                ]
                            },
                            "$logs.work_hours",
                            None,
                        ]
                    }
                },
            }
        },
        {
            "$project": {
                "_id": 0,
                "department": "$_id",
                "headcount": {"$size": "$employees"},
                "present_days": 1,
                "late_count": 1,
                "total_late_minutes": 1,
                "leave_count": 1,
                "on_duty_count": 1,
                "avg_work_hours": {
                    "$let": {
                        "vars": {
                            "valid_hours": {
                                "$filter": {
                                    "input": "$work_hours",
                                    "as": "hours",
                                    "cond": {"$ne": ["$$hours", None]},
                                }
                            }
                        },
                        "in": {
                            "$cond": [
                                {"$gt": [{"$size": "$$valid_hours"}, 0]},
                                {"$avg": "$$valid_hours"},
                                None,
                            ]
                        },
                    }
                },
            }
        },
        {"$sort": {"department": 1}},
    ]


def build_department_trend_pipeline(
    department: str, date_from: str, date_to: str
):
    start, end = parse_date(date_from), parse_date(date_to)

    pipeline = [
        {"$match": {"department": department}},
        {
            "$lookup": {
                "from": "attendance_logs",
                "let": {"code": "$emp_code"},
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {
                                "$and": [
                                    {"$eq": ["$emp_code", "$$code"]},
                                    {"$gte": ["$date", date_from]},
                                    {"$lte": ["$date", date_to]},
                                ]
                            }
                        }
                    }
                ],
                "as": "logs",
            }
        },
        {"$unwind": {"path": "$logs", "preserveNullAndEmptyArrays": True}},
        {
            "$group": {
                "_id": "$logs.date",
                "present_count": {
                    "$sum": {
                        "$cond": [
                            {"$in": ["$logs.status", list(PRESENCE)]},
                            {
                                "$cond": [
                                    {"$eq": ["$logs.half_day", True]},
                                    0.5,
                                    1,
                                ]
                            },
                            0,
                        ]
                    }
                },
                "late_count": {
                    "$sum": {
                        "$cond": [
                            {"$gt": ["$logs.late_minutes", 0]},
                            1,
                            0,
                        ]
                    }
                },
            }
        },
        {
            "$project": {
                "_id": 0,
                "date": {"$ifNull": ["$_id", date_from]},
                "present_count": 1,
                "late_count": 1,
            }
        },
        {"$set": {"day": {"$dateFromString": {"dateString": "$date"}}}},
        {
            "$densify": {
                "field": "day",
                "range": {
                    "step": 1,
                    "unit": "day",
                    "bounds": [
                        datetime.combine(start, time.min),
                        datetime.combine(end + timedelta(days=1), time.min),
                    ],
                },
            }
        },
        {
            "$set": {
                "date": {
                    "$dateToString": {
                        "format": "%Y-%m-%d",
                        "date": "$day",
                    }
                }
            }
        },
        {
            "$set": {
                "present_count": {"$ifNull": ["$present_count", 0]},
                "late_count": {"$ifNull": ["$late_count", 0]},
                "is_working_day": {
                    "$and": [
                        {"$gte": [{"$dayOfWeek": "$day"}, 2]},
                        {"$lte": [{"$dayOfWeek": "$day"}, 6]},
                    ]
                },
            }
        },
        {
            "$lookup": {
                "from": "employees",
                "let": {"day_string": "$date"},
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {
                                "$and": [
                                    {"$eq": ["$department", department]},
                                    {"$lte": ["$joined_on", "$$day_string"]},
                                ]
                            }
                        }
                    },
                    {"$count": "n"},
                ],
                "as": "headcount_data",
            }
        },
        {
            "$set": {
                "headcount": {
                    "$ifNull": [
                        {"$arrayElemAt": ["$headcount_data.n", 0]},
                        0,
                    ]
                }
            }
        },
        {
            "$set": {
                "attendance_rate": {
                    "$cond": [
                        {
                            "$and": [
                                "$is_working_day",
                                {"$gt": ["$headcount", 0]},
                            ]
                        },
                        {
                            "$round": [
                                {"$divide": ["$present_count", "$headcount"]},
                                4,
                            ]
                        },
                        None,
                    ]
                }
            }
        },
        {
            "$setWindowFields": {
                "sortBy": {"day": 1},
                "output": {
                    "moving_avg_7d": {
                        "$avg": "$attendance_rate",
                        "window": {"documents": [-6, 0]},
                    }
                },
            }
        },
        {
            "$project": {
                "_id": 0,
                "date": 1,
                "is_working_day": 1,
                "headcount": 1,
                "present_count": 1,
                "late_count": 1,
                "attendance_rate": 1,
                "moving_avg_7d": {"$round": ["$moving_avg_7d", 4]},
            }
        },
        {"$sort": {"date": 1}},
    ]

    return pipeline


@app.get("/analytics/departments/{department}/trend")
def department_trend(department: str, date_from: str, date_to: str):
    start, end = parse_date(date_from), parse_date(date_to)

    if end < start:
        fail(422, "date_to must be on or after date_from")

    if (end - start).days + 1 > 92:
        fail(422, "Date range cannot exceed 92 days")

    if db.employees.count_documents({"department": department}) == 0:
        fail(404, "Department not found")

    pipeline = build_department_trend_pipeline(
        department, date_from, date_to
    )

    return {
        "department": department,
        "date_from": date_from,
        "date_to": date_to,
        "items": list(
            db.employees.aggregate(pipeline, allowDiskUse=True)
        ),
    }


# ---------------------------------------------------------------------------
# Explain endpoint
# ---------------------------------------------------------------------------




@app.get("/admin/explain/{endpoint}")
def explain_endpoint(
    endpoint: Literal[
        "attendance_list",
        "employee_monthly",
        "department_summary",
        "late_leaderboard",
        "department_trend",
    ],
    emp_code: Optional[str] = None,
    month: Optional[str] = None,
    department: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    limit: int = Query(10, ge=1, le=1000),
):
    # 1. Attendance list explain
    if endpoint == "attendance_list":
        start = parse_date(date_from) if date_from else None
        end = parse_date(date_to) if date_to else None

        if start and end and start > end:
            fail(422, "date_from must be on or before date_to")

        q = add_attendance_filters(
            emp_code, date_from, date_to, status
        )

        pipeline = [
            {"$match": q},
            {"$sort": {"date": -1, "emp_code": 1}},
            {"$skip": (page - 1) * page_size},
            {"$limit": page_size},
        ]

        return mongo_explain(db.attendance_logs, pipeline)

    # 2. Department trend explain
    if endpoint == "department_trend":
        if not department or not date_from or not date_to:
            fail(
                422,
                "department, date_from and date_to are required",
            )

        start = parse_date(date_from)
        end = parse_date(date_to)

        if end < start:
            fail(422, "date_to must be on or after date_from")

        if (end - start).days + 1 > 92:
            fail(422, "Date range cannot exceed 92 days")

        if db.employees.count_documents(
            {"department": department}
        ) == 0:
            fail(404, "Department not found")

        pipeline = build_department_trend_pipeline(
            department, date_from, date_to
        )

        return mongo_explain(db.employees, pipeline)

    # Remaining analytics explain endpoints require a month.
    if not month:
        fail(422, "month is required for this endpoint")

    first, last = parse_month(month)

    # 3. Employee monthly analytics explain
    if endpoint == "employee_monthly":
        if not emp_code:
            fail(422, "emp_code is required")

        require_employee(emp_code)

        pipeline = [
            {
                "$match": {
                    "emp_code": emp_code,
                    "date": {
                        "$gte": first.isoformat(),
                        "$lte": last.isoformat(),
                    },
                }
            }
        ]

        return mongo_explain(db.attendance_logs, pipeline)

    # 4. Department summary explain
    if endpoint == "department_summary":
        pipeline = build_department_summary_pipeline(month)

        return mongo_explain(db.employees, pipeline)

    # 5. Late leaderboard explain
    if endpoint == "late_leaderboard":
        pipeline = build_late_leaderboard_pipeline(
            first=first,
            last=last,
            department=department,
            limit=limit,
        )

        return mongo_explain(db.attendance_logs, pipeline)

    fail(422, "Unsupported endpoint")


# --------------------------------------------------------------------------- #
# TODO - the rest of the contract (see openapi.yaml):
#   POST  /attendance/punch-out
#   PATCH /attendance/{emp_code}/{date}
#   GET   /analytics/employees/{emp_code}/monthly
#   GET   /analytics/departments/summary
#   GET   /analytics/leaderboard/late
#   GET   /analytics/departments/{department}/trend
#   GET   /admin/explain/{endpoint}
# --------------------------------------------------------------------------- #


