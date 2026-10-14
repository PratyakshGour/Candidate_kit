"""
Employee Attendance & Analytics API - STARTER

Run:  uvicorn app.main:app --port 8000
Env:  MONGO_URI, MONGO_DB (a local .env is loaded for convenience)

This file was written quickly by a colleague who has left the company. The happy path
works, but nobody has reviewed it. Read PROBLEM_STATEMENT.docx for what is expected of you,
openapi.yaml for the contract and DATA_MODEL.md for what is stored in MongoDB.
"""
import os
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Optional, List, Literal

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query, status
from pydantic import BaseModel, Field, field_validator, model_validator
import pymongo
from pymongo import MongoClient, ReturnDocument
from pymongo.errors import DuplicateKeyError

ASCENDING = 1
DESCENDING = -1

load_dotenv()

client = MongoClient(os.getenv("MONGO_URI", "mongodb://localhost:27017"))
db = client[os.getenv("MONGO_DB", "attendance_db")]

IST = timezone(timedelta(hours=5, minutes=30))


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Defect 5: Create all indexes idempotently at startup
    db.employees.create_index([("emp_code", ASCENDING)], unique=True)
    db.employees.create_index([("department", ASCENDING)])
    db.attendance_logs.create_index([("emp_code", ASCENDING), ("date", ASCENDING)], unique=True)
    db.attendance_logs.create_index([("date", DESCENDING), ("emp_code", ASCENDING)])
    db.attendance_logs.create_index([("status", ASCENDING)])
    yield


app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0", lifespan=lifespan)


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def now_epoch_ms() -> int:
    """Current time as epoch milliseconds truncated to whole seconds (R1)."""
    now_dt = datetime.now(timezone.utc)
    sec = int(now_dt.timestamp())
    return sec * 1000


def epoch_ms_to_datetime(ms: int) -> datetime:
    """Convert epoch milliseconds to UTC datetime with R1 whole-second truncation."""
    sec = ms // 1000
    return datetime.fromtimestamp(sec, tz=timezone.utc)


def datetime_to_epoch_ms(dt: Optional[datetime]) -> Optional[int]:
    """Convert BSON datetime to integer epoch milliseconds."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp()) * 1000


def get_attendance_date(punch_in_dt: datetime, shift_start: str, shift_end: str) -> str:
    """
    R1 Time zone and attendance date:
    Shift times and attendance dates are IST (UTC+05:30). A record's date is the IST calendar
    date of its punch_in instant.
    Exception - overnight shifts (shift_end <= shift_start): a punch-in whose IST time is
    strictly earlier than shift_end belongs to the shift that started the previous day.
    """
    p_ist = punch_in_dt.astimezone(IST).replace(microsecond=0)
    ist_time_str = p_ist.strftime("%H:%M")

    h_end, m_end = map(int, shift_end.split(":"))
    h_start, m_start = map(int, shift_start.split(":"))

    is_overnight = (h_end, m_end) <= (h_start, m_start)
    if is_overnight and ist_time_str < shift_end:
        prev_day = p_ist - timedelta(days=1)
        return prev_day.date().isoformat()
    return p_ist.date().isoformat()


def compute_late_minutes(punch_in: datetime, shift_start: str, attendance_date: str) -> int:
    """
    R1: Instants are truncated to whole seconds. Attendance dates and shift times are IST.
    R2: Grace period is 10 minutes (600 seconds). A punch-in is late only if strictly > 10:00
        after shift_start. When late, late_minutes = floor((punch_in - shift_start) in minutes).
        Otherwise 0.
    """
    if punch_in.tzinfo is None:
        p_ist = punch_in.replace(tzinfo=timezone.utc).astimezone(IST).replace(microsecond=0)
    else:
        p_ist = punch_in.astimezone(IST).replace(microsecond=0)

    h, m = map(int, shift_start.split(":"))
    start_dt = datetime.fromisoformat(attendance_date).replace(
        hour=h, minute=m, second=0, microsecond=0, tzinfo=IST
    )

    diff_seconds = (p_ist - start_dt).total_seconds()
    if diff_seconds > 600:
        return int(diff_seconds // 60)
    return 0


def compute_work_hours(punch_in: datetime, punch_out: datetime) -> float:
    """
    R1: Instants are truncated to whole seconds.
    R4 & R8: (punch_out - punch_in) in seconds / 3600, rounded to 2 decimals half-up
             (not banker's rounding, not float rounding).
    """
    p_in = punch_in.replace(microsecond=0)
    p_out = punch_out.replace(microsecond=0)
    diff_sec = int((p_out - p_in).total_seconds())
    hours = Decimal(diff_sec) / Decimal(3600)
    rounded = hours.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    return float(rounded)


def compute_overtime(
    punch_out: datetime, shift_end: str, attendance_date: str, shift_start: Optional[str] = None
) -> int:
    """
    R1: Instants are truncated to whole seconds. Shift times and attendance dates are IST.
        Overnight shifts (shift_end <= shift_start): shift_end falls on the next calendar day.
    R3: overtime_minutes = whole minutes (floor) between shift_end and punch_out,
        counted only when that is >= 30; otherwise 0.
    """
    if punch_out.tzinfo is None:
        p_out = punch_out.replace(tzinfo=timezone.utc).astimezone(IST).replace(microsecond=0)
    else:
        p_out = punch_out.astimezone(IST).replace(microsecond=0)

    h_end, m_end = map(int, shift_end.split(":"))
    end_dt = datetime.fromisoformat(attendance_date).replace(
        hour=h_end, minute=m_end, second=0, microsecond=0, tzinfo=IST
    )

    if shift_start:
        h_start, m_start = map(int, shift_start.split(":"))
        if (h_end, m_end) <= (h_start, m_start):
            end_dt += timedelta(days=1)

    diff_seconds = (p_out - end_dt).total_seconds()
    if diff_seconds < 0:
        return 0

    minutes = int(diff_seconds // 60)
    return minutes if minutes >= 30 else 0


def format_employee_doc(doc: dict) -> dict:
    return {
        "emp_code": doc["emp_code"],
        "name": doc["name"],
        "email": doc["email"],
        "department": doc["department"],
        "shift_start": doc["shift_start"],
        "shift_end": doc["shift_end"],
        "joined_on": doc["joined_on"],
        "created_at": datetime_to_epoch_ms(doc.get("created_at")),
    }


def format_attendance_doc(doc: dict) -> dict:
    history = []
    for h in doc.get("history", []):
        h_changes = {}
        for k, v in h.get("changes", {}).items():
            from_val = v.get("from")
            to_val = v.get("to")
            if k in ("punch_in", "punch_out"):
                from_val = datetime_to_epoch_ms(from_val) if isinstance(from_val, datetime) else from_val
                to_val = datetime_to_epoch_ms(to_val) if isinstance(to_val, datetime) else to_val
            h_changes[k] = {"from": from_val, "to": to_val}
        h_at = h.get("at")
        history.append({
            "at": datetime_to_epoch_ms(h_at) if isinstance(h_at, datetime) else h_at,
            "by": h.get("by"),
            "reason": h.get("reason"),
            "changes": h_changes,
        })

    return {
        "emp_code": doc["emp_code"],
        "date": doc["date"],
        "status": doc["status"],
        "punch_in": datetime_to_epoch_ms(doc.get("punch_in")),
        "punch_out": datetime_to_epoch_ms(doc.get("punch_out")),
        "work_hours": doc.get("work_hours"),
        "late_minutes": doc.get("late_minutes", 0),
        "overtime_minutes": doc.get("overtime_minutes", 0),
        "half_day": doc.get("half_day", False),
        "history": history,
    }


# --------------------------------------------------------------------------- #
# Models (Defect 4, 6, 8)
# --------------------------------------------------------------------------- #
class EmployeeIn(BaseModel):
    emp_code: str = Field(..., pattern=r"^EMP\d{4,6}$")
    name: str = Field(..., min_length=1, max_length=100)
    email: str = Field(..., max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department: str = Field(..., min_length=1, max_length=50)
    shift_start: str = Field("09:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    shift_end: str = Field("18:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    joined_on: str = Field(..., pattern=r"^\d{4}-\d{2}-\d{2}$")

    @model_validator(mode="after")
    def validate_shift(self):
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start must differ from shift_end")
        return self


class PunchInIn(BaseModel):
    emp_code: str
    punched_at: Optional[int] = Field(None, ge=100000000000, le=4102444800000)
    status: Literal["PRESENT", "WFH", "ON_DUTY"] = "PRESENT"


# --------------------------------------------------------------------------- #
# Endpoints provided (Fixed)
# --------------------------------------------------------------------------- #
@app.get("/health")
def health():
    try:
        client.admin.command("ping")
        return {"status": "ok"}
    except Exception:
        raise HTTPException(status_code=503, detail="Database unavailable")


@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn):
    doc = body.model_dump()
    created_dt = epoch_ms_to_datetime(now_epoch_ms())
    doc["created_at"] = created_dt
    try:
        db.employees.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(status_code=409, detail="emp_code already exists")
    return format_employee_doc(doc)


@app.get("/employees")
def list_employees(
    department: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    q = {}
    if department:
        q["department"] = department
    skip = (page - 1) * page_size
    total = db.employees.count_documents(q)
    cursor = db.employees.find(q).sort("emp_code", ASCENDING).skip(skip).limit(page_size)
    items = [format_employee_doc(d) for d in cursor]
    return {"items": items, "total": total, "page": page, "page_size": page_size}


@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInIn):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(status_code=404, detail="Employee not found")

    punch_ms = body.punched_at if body.punched_at is not None else now_epoch_ms()
    ts = epoch_ms_to_datetime(punch_ms)
    attendance_date = get_attendance_date(ts, emp["shift_start"], emp["shift_end"])

    doc = {
        "emp_code": body.emp_code,
        "date": attendance_date,
        "status": body.status,
        "punch_in": ts,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": compute_late_minutes(ts, emp["shift_start"], attendance_date),
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }
    try:
        db.attendance_logs.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(status_code=409, detail="already punched in for this date")

    return format_attendance_doc(doc)


@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    if date_from and date_to and date_from > date_to:
        raise HTTPException(status_code=422, detail="date_from must not be greater than date_to")

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

    total = db.attendance_logs.count_documents(q)
    skip = (page - 1) * page_size
    cursor = (
        db.attendance_logs.find(q)
        .sort([("date", DESCENDING), ("emp_code", ASCENDING)])
        .skip(skip)
        .limit(page_size)
    )
    items = [format_attendance_doc(d) for d in cursor]
    return {"items": items, "total": total, "page": page, "page_size": page_size}


# --------------------------------------------------------------------------- #
# Models (Part B)
# --------------------------------------------------------------------------- #
class PunchOutIn(BaseModel):
    emp_code: str
    punched_at: Optional[int] = Field(None, ge=100000000000, le=4102444800000)


class RegularizeIn(BaseModel):
    status: Optional[Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]] = None
    punch_in: Optional[int] = Field(None, ge=100000000000, le=4102444800000)
    punch_out: Optional[int] = Field(None, ge=100000000000, le=4102444800000)
    reason: str = Field(..., min_length=5, max_length=200)
    regularized_by: str = Field(..., min_length=1, max_length=50)


def round_half_up(val: Optional[float], decimals: int = 2) -> Optional[float]:
    if val is None:
        return None
    d = Decimal(str(val))
    target = Decimal("0." + "0" * decimals) if decimals > 0 else Decimal("1")
    return float(d.quantize(target, rounding=ROUND_HALF_UP))


# --------------------------------------------------------------------------- #
# Part B: Attendance Workflow Endpoints
# --------------------------------------------------------------------------- #
@app.post("/attendance/punch-out", status_code=200)
def punch_out(body: PunchOutIn):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(status_code=404, detail="Employee not found")

    punch_ms = body.punched_at if body.punched_at is not None else now_epoch_ms()
    punch_out_dt = epoch_ms_to_datetime(punch_ms)

    record = db.attendance_logs.find_one(
        {"emp_code": body.emp_code, "punch_in": {"$lte": punch_out_dt}},
        sort=[("punch_in", DESCENDING)],
    )
    if not record:
        raise HTTPException(status_code=404, detail="No punch-in found")

    if record.get("punch_out") is not None:
        raise HTTPException(status_code=409, detail="already punched out")

    p_in = record["punch_in"]
    if p_in.tzinfo is None:
        p_in = p_in.replace(tzinfo=timezone.utc)

    diff_sec = (punch_out_dt - p_in).total_seconds()
    if diff_sec <= 0 or diff_sec > 86400:
        raise HTTPException(
            status_code=422, detail="punched_at must be after punch_in and within 24 hours"
        )

    work_hours = compute_work_hours(p_in, punch_out_dt)
    overtime_minutes = compute_overtime(
        punch_out_dt, emp["shift_end"], record["date"], emp["shift_start"]
    )
    half_day = bool(work_hours < 4.50)

    res = db.attendance_logs.find_one_and_update(
        {"_id": record["_id"], "punch_out": None},
        {
            "$set": {
                "punch_out": punch_out_dt,
                "work_hours": work_hours,
                "overtime_minutes": overtime_minutes,
                "half_day": half_day,
            }
        },
        return_document=pymongo.ReturnDocument.AFTER,
    )
    if not res:
        raise HTTPException(status_code=409, detail="already punched out")

    return format_attendance_doc(res)


@app.patch("/attendance/{emp_code}/{date}", status_code=200)
def regularize_attendance(emp_code: str, date: str, body: RegularizeIn):
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(status_code=404, detail="Employee not found")

    record = db.attendance_logs.find_one({"emp_code": emp_code, "date": date})
    if not record:
        raise HTTPException(status_code=404, detail="Attendance record not found")

    cur_status = record.get("status")
    cur_punch_in = record.get("punch_in")
    cur_punch_out = record.get("punch_out")
    cur_work_hours = record.get("work_hours")
    cur_late_minutes = record.get("late_minutes", 0)
    cur_overtime_minutes = record.get("overtime_minutes", 0)
    cur_half_day = record.get("half_day", False)

    target_status = body.status if body.status is not None else cur_status

    if body.status in ("ABSENT", "LEAVE") and (
        body.punch_in is not None or body.punch_out is not None
    ):
        raise HTTPException(
            status_code=422,
            detail="punch_in and punch_out cannot be supplied when setting status to ABSENT or LEAVE",
        )

    if cur_status in ("ABSENT", "LEAVE") and body.status is None and (
        body.punch_in is not None or body.punch_out is not None
    ):
        raise HTTPException(
            status_code=422, detail="punch times not allowed for ABSENT or LEAVE records"
        )

    if target_status in ("ABSENT", "LEAVE"):
        target_punch_in = None
        target_punch_out = None
    else:
        target_punch_in = (
            epoch_ms_to_datetime(body.punch_in)
            if body.punch_in is not None
            else cur_punch_in
        )
        target_punch_out = (
            epoch_ms_to_datetime(body.punch_out)
            if body.punch_out is not None
            else cur_punch_out
        )

    if target_status in ("PRESENT", "WFH", "ON_DUTY") and target_punch_in is None:
        raise HTTPException(
            status_code=422, detail="a presence status requires a punch_in"
        )

    if target_punch_in is not None:
        if get_attendance_date(target_punch_in, emp["shift_start"], emp["shift_end"]) != date:
            raise HTTPException(
                status_code=422, detail="punch_in must stay on the record's attendance date"
            )

    if target_punch_out is not None:
        if target_punch_in is None:
            raise HTTPException(status_code=422, detail="punch_out requires punch_in")
        p_in_tz = target_punch_in if target_punch_in.tzinfo else target_punch_in.replace(tzinfo=timezone.utc)
        p_out_tz = target_punch_out if target_punch_out.tzinfo else target_punch_out.replace(tzinfo=timezone.utc)
        diff_sec = (p_out_tz - p_in_tz).total_seconds()
        if diff_sec <= 0 or diff_sec > 86400:
            raise HTTPException(
                status_code=422,
                detail="punch_out must be after punch_in and within 24 hours",
            )

    if target_status in ("ABSENT", "LEAVE"):
        target_work_hours = None
        target_late_minutes = 0
        target_overtime_minutes = 0
        target_half_day = False
    else:
        target_late_minutes = compute_late_minutes(target_punch_in, emp["shift_start"], date)
        if target_punch_out is not None:
            target_work_hours = compute_work_hours(target_punch_in, target_punch_out)
            target_overtime_minutes = compute_overtime(
                target_punch_out, emp["shift_end"], date, emp["shift_start"]
            )
            target_half_day = bool(target_work_hours < 4.50)
        else:
            target_work_hours = None
            target_overtime_minutes = 0
            target_half_day = False

    changes_db = {}
    if cur_status != target_status:
        changes_db["status"] = {"from": cur_status, "to": target_status}
    if cur_punch_in != target_punch_in:
        changes_db["punch_in"] = {"from": cur_punch_in, "to": target_punch_in}
    if cur_punch_out != target_punch_out:
        changes_db["punch_out"] = {"from": cur_punch_out, "to": target_punch_out}
    if cur_work_hours != target_work_hours:
        changes_db["work_hours"] = {"from": cur_work_hours, "to": target_work_hours}
    if cur_late_minutes != target_late_minutes:
        changes_db["late_minutes"] = {"from": cur_late_minutes, "to": target_late_minutes}
    if cur_overtime_minutes != target_overtime_minutes:
        changes_db["overtime_minutes"] = {"from": cur_overtime_minutes, "to": target_overtime_minutes}
    if cur_half_day != target_half_day:
        changes_db["half_day"] = {"from": cur_half_day, "to": target_half_day}

    if not changes_db:
        raise HTTPException(status_code=422, detail="Request changes nothing")

    now_dt = epoch_ms_to_datetime(now_epoch_ms())
    history_entry = {
        "at": now_dt,
        "by": body.regularized_by,
        "reason": body.reason,
        "changes": changes_db,
    }

    cur_history = record.get("history", [])
    updated = db.attendance_logs.find_one_and_update(
        {"_id": record["_id"], "history": cur_history},
        {
            "$set": {
                "status": target_status,
                "punch_in": target_punch_in,
                "punch_out": target_punch_out,
                "work_hours": target_work_hours,
                "late_minutes": target_late_minutes,
                "overtime_minutes": target_overtime_minutes,
                "half_day": target_half_day,
            },
            "$push": {"history": history_entry},
        },
        return_document=pymongo.ReturnDocument.AFTER,
    )
    if not updated:
        raise HTTPException(
            status_code=409, detail="Concurrent edit conflict on attendance record"
        )

    return format_attendance_doc(updated)


# --------------------------------------------------------------------------- #
# Part B: MongoDB Aggregation Pipelines
# --------------------------------------------------------------------------- #
def build_employee_monthly_pipeline(emp_code: str, month: str):
    import calendar

    year, mon = map(int, month.split("-"))
    _, num_days = calendar.monthrange(year, mon)
    first_day_str = f"{month}-01"
    last_day_str = f"{month}-{num_days:02d}"

    return [
        {
            "$match": {
                "emp_code": emp_code,
                "date": {"$gte": first_day_str, "$lte": last_day_str},
            }
        },
        {
            "$project": {
                "status": 1,
                "late_minutes": {"$ifNull": ["$late_minutes", 0]},
                "overtime_minutes": {"$ifNull": ["$overtime_minutes", 0]},
                "half_day": {"$ifNull": ["$half_day", False]},
                "dayOfWeek": {"$dayOfWeek": {"$dateFromString": {"dateString": "$date"}}},
            }
        },
        {
            "$addFields": {
                "is_weekday": {"$in": ["$dayOfWeek", [2, 3, 4, 5, 6]]},
                "is_presence": {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                "is_leave": {"$eq": ["$status", "LEAVE"]},
            }
        },
        {
            "$group": {
                "_id": None,
                "present_days": {
                    "$sum": {
                        "$cond": [
                            {"$and": ["$is_presence", "$is_weekday"]},
                            {"$cond": ["$half_day", 0.5, 1.0]},
                            0.0,
                        ]
                    }
                },
                "leave_days": {"$sum": {"$cond": ["$is_leave", 1, 0]}},
                "late_count": {"$sum": {"$cond": [{"$gt": ["$late_minutes", 0]}, 1, 0]}},
                "total_late_minutes": {"$sum": "$late_minutes"},
                "total_overtime_minutes": {"$sum": "$overtime_minutes"},
            }
        },
    ]


@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(
    emp_code: str,
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
):
    import calendar

    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(status_code=404, detail="Employee not found")

    year, mon = map(int, month.split("-"))
    _, num_days = calendar.monthrange(year, mon)
    first_day_str = f"{month}-01"
    last_day_str = f"{month}-{num_days:02d}"

    emp_joined = emp["joined_on"]
    start_date_str = max(first_day_str, emp_joined)
    working_days = 0
    if start_date_str <= last_day_str:
        start_d = datetime.fromisoformat(start_date_str).date()
        end_d = datetime.fromisoformat(last_day_str).date()
        curr_d = start_d
        while curr_d <= end_d:
            if curr_d.weekday() < 5:
                working_days += 1
            curr_d += timedelta(days=1)

    pipeline = build_employee_monthly_pipeline(emp_code, month)
    res = list(db.attendance_logs.aggregate(pipeline))
    if res:
        row = res[0]
        present_days = float(row.get("present_days", 0.0))
        leave_days = int(row.get("leave_days", 0))
        late_count = int(row.get("late_count", 0))
        total_late_minutes = int(row.get("total_late_minutes", 0))
        total_overtime_minutes = int(row.get("total_overtime_minutes", 0))
    else:
        present_days = 0.0
        leave_days = 0
        late_count = 0
        total_late_minutes = 0
        total_overtime_minutes = 0

    if working_days == 0:
        attendance_pct = None
    else:
        attendance_pct = round_half_up(present_days * 100.0 / working_days, 2)

    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": working_days,
        "present_days": present_days,
        "leave_days": leave_days,
        "late_count": late_count,
        "total_late_minutes": total_late_minutes,
        "total_overtime_minutes": total_overtime_minutes,
        "attendance_pct": attendance_pct,
    }


def build_department_summary_pipeline(month: str, department: Optional[str] = None):
    import calendar

    year, mon = map(int, month.split("-"))
    _, num_days = calendar.monthrange(year, mon)
    first_day_str = f"{month}-01"
    last_day_str = f"{month}-{num_days:02d}"

    emp_match = {"joined_on": {"$lte": last_day_str}}
    if department:
        emp_match["department"] = department

    return [
        {"$match": emp_match},
        {
            "$lookup": {
                "from": "attendance_logs",
                "let": {"emp": "$emp_code"},
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {
                                "$and": [
                                    {"$eq": ["$emp_code", "$$emp"]},
                                    {"$gte": ["$date", first_day_str]},
                                    {"$lte": ["$date", last_day_str]},
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
                "emp_set": {"$addToSet": "$emp_code"},
                "present_days": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$in": ["$logs.status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                    {
                                        "$in": [
                                            {
                                                "$dayOfWeek": {
                                                    "$dateFromString": {
                                                        "dateString": "$logs.date"
                                                    }
                                                }
                                            },
                                            [2, 3, 4, 5, 6],
                                        ]
                                    },
                                ]
                            },
                            {"$cond": ["$logs.half_day", 0.5, 1.0]},
                            0.0,
                        ]
                    }
                },
                "work_hours_sum": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$in": ["$logs.status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                    {"$ne": ["$logs.work_hours", None]},
                                ]
                            },
                            "$logs.work_hours",
                            0.0,
                        ]
                    }
                },
                "work_hours_count": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$in": ["$logs.status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                    {"$ne": ["$logs.work_hours", None]},
                                ]
                            },
                            1,
                            0,
                        ]
                    }
                },
                "late_count": {
                    "$sum": {"$cond": [{"$gt": ["$logs.late_minutes", 0]}, 1, 0]}
                },
                "total_late_minutes": {
                    "$sum": {"$ifNull": ["$logs.late_minutes", 0]}
                },
                "leave_count": {
                    "$sum": {"$cond": [{"$eq": ["$logs.status", "LEAVE"]}, 1, 0]}
                },
                "on_duty_count": {
                    "$sum": {"$cond": [{"$eq": ["$logs.status", "ON_DUTY"]}, 1, 0]}
                },
            }
        },
        {
            "$project": {
                "_id": 0,
                "department": "$_id",
                "headcount": {"$size": "$emp_set"},
                "present_days": "$present_days",
                "avg_work_hours": {
                    "$cond": [
                        {"$gt": ["$work_hours_count", 0]},
                        {"$divide": ["$work_hours_sum", "$work_hours_count"]},
                        None,
                    ]
                },
                "late_count": "$late_count",
                "total_late_minutes": "$total_late_minutes",
                "leave_count": "$leave_count",
                "on_duty_count": "$on_duty_count",
            }
        },
        {"$match": {"headcount": {"$gt": 0}}},
        {"$sort": {"department": 1}},
    ]


@app.get("/analytics/departments/summary")
def department_summary(
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    department: Optional[str] = None,
):
    pipeline = build_department_summary_pipeline(month, department)
    results = list(db.employees.aggregate(pipeline))
    items = []
    for r in results:
        items.append({
            "department": r["department"],
            "headcount": int(r["headcount"]),
            "present_days": float(r["present_days"]),
            "avg_work_hours": round_half_up(r["avg_work_hours"], 2),
            "late_count": int(r["late_count"]),
            "total_late_minutes": int(r["total_late_minutes"]),
            "leave_count": int(r["leave_count"]),
            "on_duty_count": int(r["on_duty_count"]),
        })
    return {"month": month, "items": items}


def build_late_leaderboard_pipeline(
    month: str, limit: int = 10, department: Optional[str] = None
):
    import calendar

    year, mon = map(int, month.split("-"))
    _, num_days = calendar.monthrange(year, mon)
    first_day_str = f"{month}-01"
    last_day_str = f"{month}-{num_days:02d}"

    pipeline = [
        {
            "$match": {
                "date": {"$gte": first_day_str, "$lte": last_day_str},
                "late_minutes": {"$gt": 0},
            }
        },
        {
            "$group": {
                "_id": "$emp_code",
                "total_late_minutes": {"$sum": "$late_minutes"},
                "late_count": {"$sum": 1},
            }
        },
        {
            "$lookup": {
                "from": "employees",
                "localField": "_id",
                "foreignField": "emp_code",
                "as": "emp",
            }
        },
        {"$unwind": "$emp"},
    ]

    if department:
        pipeline.append({"$match": {"emp.department": department}})

    pipeline.extend([
        {
            "$project": {
                "_id": 0,
                "emp_code": "$_id",
                "name": "$emp.name",
                "department": "$emp.department",
                "total_late_minutes": "$total_late_minutes",
                "late_count": "$late_count",
            }
        },
        {
            "$setWindowFields": {
                "sortBy": {"total_late_minutes": -1},
                "output": {"rank": {"$rank": {}}},
            }
        },
        {"$match": {"rank": {"$lte": limit}}},
        {"$sort": {"total_late_minutes": -1, "emp_code": 1}},
    ])
    return pipeline


@app.get("/analytics/leaderboard/late")
def late_leaderboard(
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    limit: int = Query(10, ge=1, le=50),
    department: Optional[str] = None,
):
    pipeline = build_late_leaderboard_pipeline(month, limit, department)
    results = list(db.attendance_logs.aggregate(pipeline))
    items = []
    for r in results:
        items.append({
            "rank": int(r["rank"]),
            "emp_code": r["emp_code"],
            "name": r["name"],
            "department": r["department"],
            "total_late_minutes": int(r["total_late_minutes"]),
            "late_count": int(r["late_count"]),
        })
    return {"month": month, "items": items}


def build_department_trend_pipeline(department: str, from_date: str, to_date: str):
    start_dt = datetime.fromisoformat(from_date)
    end_dt = datetime.fromisoformat(to_date)
    days = (end_dt.date() - start_dt.date()).days + 1

    return [
        {"$match": {"department": department}},
        {
            "$group": {
                "_id": "$department",
                "employees": {
                    "$push": {"emp_code": "$emp_code", "joined_on": "$joined_on"}
                },
                "emp_codes": {"$push": "$emp_code"},
            }
        },
        {
            "$lookup": {
                "from": "attendance_logs",
                "let": {"codes": "$emp_codes"},
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {
                                "$and": [
                                    {"$in": ["$emp_code", "$$codes"]},
                                    {"$gte": ["$date", from_date]},
                                    {"$lte": ["$date", to_date]},
                                ]
                            }
                        }
                    }
                ],
                "as": "logs",
            }
        },
        {"$addFields": {"day_indices": {"$range": [0, days]}}},
        {"$unwind": "$day_indices"},
        {
            "$addFields": {
                "date_dt": {
                    "$dateAdd": {
                        "startDate": start_dt,
                        "unit": "day",
                        "amount": "$day_indices",
                    }
                }
            }
        },
        {
            "$addFields": {
                "date": {"$dateToString": {"format": "%Y-%m-%d", "date": "$date_dt"}},
                "is_working_day": {"$in": [{"$dayOfWeek": "$date_dt"}, [2, 3, 4, 5, 6]]},
                "headcount": {
                    "$size": {
                        "$filter": {
                            "input": "$employees",
                            "as": "e",
                            "cond": {
                                "$lte": [
                                    "$$e.joined_on",
                                    {
                                        "$dateToString": {
                                            "format": "%Y-%m-%d",
                                            "date": "$date_dt",
                                        }
                                    },
                                ]
                            },
                        }
                    }
                },
                "day_logs": {
                    "$filter": {
                        "input": "$logs",
                        "as": "l",
                        "cond": {
                            "$eq": [
                                "$$l.date",
                                {
                                    "$dateToString": {
                                        "format": "%Y-%m-%d",
                                        "date": "$date_dt",
                                    }
                                },
                            ]
                        },
                    }
                },
            }
        },
        {
            "$addFields": {
                "present_count": {
                    "$sum": {
                        "$map": {
                            "input": "$day_logs",
                            "as": "l",
                            "in": {
                                "$cond": [
                                    {"$in": ["$$l.status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                    {"$cond": ["$$l.half_day", 0.5, 1.0]},
                                    0.0,
                                ]
                            },
                        }
                    }
                },
                "late_count": {
                    "$size": {
                        "$filter": {
                            "input": "$day_logs",
                            "as": "l",
                            "cond": {"$gt": ["$$l.late_minutes", 0]},
                        }
                    }
                },
            }
        },
        {
            "$addFields": {
                "attendance_rate": {
                    "$cond": [
                        {"$and": ["$is_working_day", {"$gt": ["$headcount", 0]}]},
                        {"$round": [{"$divide": ["$present_count", "$headcount"]}, 4]},
                        None,
                    ]
                }
            }
        },
        {
            "$setWindowFields": {
                "sortBy": {"date": 1},
                "output": {
                    "moving_avg_7d_raw": {
                        "$avg": "$attendance_rate",
                        "window": {"documents": [-6, "current"]},
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
                "moving_avg_7d": {
                    "$cond": [
                        {"$ne": ["$moving_avg_7d_raw", None]},
                        {"$round": ["$moving_avg_7d_raw", 4]},
                        None,
                    ]
                },
            }
        },
        {"$sort": {"date": 1}},
    ]


@app.get("/analytics/departments/{department}/trend")
def department_trend(
    department: str,
    from_date: str = Query(..., alias="from", pattern=r"^\d{4}-\d{2}-\d{2}$"),
    to_date: str = Query(..., alias="to", pattern=r"^\d{4}-\d{2}-\d{2}$"),
):
    dept_emp = db.employees.find_one({"department": department})
    if not dept_emp:
        raise HTTPException(status_code=404, detail="Department not found")

    if from_date > to_date:
        raise HTTPException(
            status_code=422, detail="'to' must not be earlier than 'from'"
        )

    start_d = datetime.fromisoformat(from_date).date()
    end_d = datetime.fromisoformat(to_date).date()
    days = (end_d - start_d).days + 1
    if days > 92:
        raise HTTPException(status_code=422, detail="Date range must not exceed 92 days")

    pipeline = build_department_trend_pipeline(department, from_date, to_date)
    results = list(db.employees.aggregate(pipeline))
    items = []
    for r in results:
        items.append({
            "date": r["date"],
            "is_working_day": bool(r["is_working_day"]),
            "headcount": int(r["headcount"]),
            "present_count": float(r["present_count"]),
            "late_count": int(r["late_count"]),
            "attendance_rate": round_half_up(r.get("attendance_rate"), 4),
            "moving_avg_7d": round_half_up(r.get("moving_avg_7d"), 4),
        })
    return {"department": department, "items": items}


# --------------------------------------------------------------------------- #
# Part C: Admin Explain Endpoint
# --------------------------------------------------------------------------- #
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
    from_date: Optional[str] = Query(None, alias="from"),
    to_date: Optional[str] = Query(None, alias="to"),
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    limit: int = Query(10, ge=1, le=50),
):
    if endpoint == "attendance_list":
        q = {}
        if emp_code:
            q["emp_code"] = emp_code
        d_from = date_from or from_date
        d_to = date_to or to_date
        if d_from or d_to:
            q["date"] = {}
            if d_from:
                q["date"]["$gte"] = d_from
            if d_to:
                q["date"]["$lte"] = d_to
        if status:
            q["status"] = status
        skip = (page - 1) * page_size
        find_cmd = {
            "find": "attendance_logs",
            "filter": q,
            "sort": {"date": DESCENDING, "emp_code": ASCENDING},
            "skip": skip,
            "limit": page_size,
        }
        plan = db.command("explain", find_cmd, verbosity="executionStats")
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": plan}

    elif endpoint == "employee_monthly":
        if not emp_code or not month:
            raise HTTPException(
                status_code=422,
                detail="emp_code and month are required for employee_monthly explain",
            )
        pipeline = build_employee_monthly_pipeline(emp_code, month)
        agg_cmd = {
            "aggregate": "attendance_logs",
            "pipeline": pipeline,
            "cursor": {},
        }
        plan = db.command("explain", agg_cmd, verbosity="executionStats")
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": plan}

    elif endpoint == "department_summary":
        if not month:
            raise HTTPException(
                status_code=422,
                detail="month is required for department_summary explain",
            )
        pipeline = build_department_summary_pipeline(month, department)
        agg_cmd = {
            "aggregate": "employees",
            "pipeline": pipeline,
            "cursor": {},
        }
        plan = db.command("explain", agg_cmd, verbosity="executionStats")
        return {"endpoint": endpoint, "collection": "employees", "explain": plan}

    elif endpoint == "late_leaderboard":
        if not month:
            raise HTTPException(
                status_code=422,
                detail="month is required for late_leaderboard explain",
            )
        pipeline = build_late_leaderboard_pipeline(month, limit, department)
        agg_cmd = {
            "aggregate": "attendance_logs",
            "pipeline": pipeline,
            "cursor": {},
        }
        plan = db.command("explain", agg_cmd, verbosity="executionStats")
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": plan}

    elif endpoint == "department_trend":
        d_from = from_date or date_from
        d_to = to_date or date_to
        if not department or not d_from or not d_to:
            raise HTTPException(
                status_code=422,
                detail="department, from, and to are required for department_trend explain",
            )
        pipeline = build_department_trend_pipeline(department, d_from, d_to)
        agg_cmd = {
            "aggregate": "employees",
            "pipeline": pipeline,
            "cursor": {},
        }
        plan = db.command("explain", agg_cmd, verbosity="executionStats")
        return {"endpoint": endpoint, "collection": "employees", "explain": plan}

    raise HTTPException(status_code=422, detail=f"Unknown endpoint: {endpoint}")

