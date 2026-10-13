"""Employee Attendance & Analytics API. All instants are UTC BSON dates / epoch ms at HTTP boundaries."""
import os, re, calendar
from datetime import datetime, date, time, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Optional, Literal

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query, Path
from pydantic import BaseModel, ConfigDict, Field, EmailStr, field_validator, model_validator
from pymongo import MongoClient, ASCENDING, DESCENDING, ReturnDocument
from pymongo.errors import DuplicateKeyError, PyMongoError
from bson import json_util

load_dotenv()
MONGO_URI = os.getenv("MONGO_URI", "mongodb://localhost:27017")
MONGO_DB = os.getenv("MONGO_DB", "attendance_db")
client = MongoClient(MONGO_URI, tz_aware=True, tzinfo=timezone.utc, serverSelectionTimeoutMS=3000)
db = client[MONGO_DB]
IST = timezone(timedelta(hours=5, minutes=30))
UTC = timezone.utc
PRESENCE = {"PRESENT", "WFH", "ON_DUTY"}
STATUSES = PRESENCE | {"ABSENT", "LEAVE"}
EPOCH_MIN, EPOCH_MAX = 100_000_000_000, 4_102_444_800_000
app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0")

@app.on_event("startup")
def create_indexes():
    db.employees.create_index([("emp_code", ASCENDING)], unique=True, name="uq_employee_code")
    db.employees.create_index([("department", ASCENDING), ("emp_code", ASCENDING)], name="department_employee")
    db.employees.create_index([("department", ASCENDING), ("joined_on", ASCENDING)], name="department_joined")
    db.employees.create_index([("joined_on", ASCENDING)], name="employee_joined_on")
    db.attendance_logs.create_index([("emp_code", ASCENDING), ("date", ASCENDING)], unique=True, name="uq_attendance_employee_date")
    db.attendance_logs.create_index([("date", DESCENDING), ("emp_code", ASCENDING)], name="attendance_date_employee")
    db.attendance_logs.create_index([("date", ASCENDING), ("status", ASCENDING), ("emp_code", ASCENDING)], name="attendance_date_status_employee")

# ------------------------------- validation --------------------------------
def parse_date(s: str, field="date") -> date:
    try:
        d = date.fromisoformat(s)
        if d.isoformat() != s: raise ValueError()
        return d
    except Exception: raise HTTPException(422, f"{field} must be YYYY-MM-DD")

def parse_month(s: str) -> tuple[date,date]:
    if not re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", s or ""):
        raise HTTPException(422, "month must be YYYY-MM")
    y,m=map(int,s.split("-")); return date(y,m,1), date(y,m,calendar.monthrange(y,m)[1])

def epoch_dt(v: int | None, *, default_now=False) -> datetime:
    if v is None and default_now: return truncate_seconds(datetime.now(UTC))
    if type(v) is not int or not EPOCH_MIN <= v <= EPOCH_MAX:
        raise HTTPException(422, "timestamp must be an integer epoch-milliseconds value in range")
    return datetime.fromtimestamp(v / 1000, UTC).replace(microsecond=0)

def truncate_seconds(dt: datetime) -> datetime:
    if dt.tzinfo is None: dt=dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC).replace(microsecond=0)

def to_ms(dt):
    if dt is None: return None
    if dt.tzinfo is None: dt=dt.replace(tzinfo=UTC)
    return int(dt.timestamp() * 1000)

def round_half_up(value, places=2):
    q=Decimal("1").scaleb(-places)
    return float(Decimal(str(value)).quantize(q, rounding=ROUND_HALF_UP))

def shift_datetime(day: date, hhmm: str) -> datetime:
    h,m=map(int,hhmm.split(":"))
    return datetime.combine(day,time(h,m),IST).astimezone(UTC).replace(microsecond=0)

def attendance_day(punch_in: datetime, emp: dict) -> date:
    local=punch_in.astimezone(IST); local_day=local.date()
    start_h,start_m=map(int,emp["shift_start"].split(":")); end_h,end_m=map(int,emp["shift_end"].split(":"))
    start_minutes=start_h*60+start_m; end_minutes=end_h*60+end_m
    if end_minutes <= start_minutes and local.hour*60+local.minute < end_minutes:
        return local_day-timedelta(days=1)
    return local_day

def compute_late_minutes(punch_in: datetime, shift_start: str, day: date | None = None) -> int:
    local=punch_in.astimezone(IST); day=day or local.date()
    start=shift_datetime(day, shift_start)
    elapsed=int((punch_in-start).total_seconds()//60)
    return elapsed if (punch_in-start).total_seconds() > 600 else 0

def shift_end_instant(day: date, emp: dict) -> datetime:
    start_h,start_m=map(int,emp["shift_start"].split(":")); end_h,end_m=map(int,emp["shift_end"].split(":"))
    end_day=day+timedelta(days=1) if end_h*60+end_m <= start_h*60+start_m else day
    return shift_datetime(end_day, emp["shift_end"])

def compute_derived(emp: dict, day: date, pin: datetime | None, pout: datetime | None):
    if pin is None: return {"late_minutes":0,"work_hours":None,"overtime_minutes":0,"half_day":False}
    late=compute_late_minutes(pin,emp["shift_start"],day)
    if pout is None: return {"late_minutes":late,"work_hours":None,"overtime_minutes":0,"half_day":False}
    seconds=int((pout-pin).total_seconds())
    hours=round_half_up(Decimal(seconds)/Decimal(3600),2)
    overtime_seconds=int((pout-shift_end_instant(day,emp)).total_seconds())
    overtime=(overtime_seconds//60) if overtime_seconds >= 1800 else 0
    return {"late_minutes":late,"work_hours":hours,"overtime_minutes":int(overtime),"half_day":hours < 4.50}

def serialize_history(history):
    out=[]
    for h in history or []:
        changes={}
        for k,v in (h.get("changes") or {}).items():
            changes[k]={"from":to_ms(v.get("from")) if k in ("punch_in","punch_out") else v.get("from"),
                        "to":to_ms(v.get("to")) if k in ("punch_in","punch_out") else v.get("to")}
        out.append({"at":to_ms(h.get("at")),"by":h.get("by"),"reason":h.get("reason"),"changes":changes})
    return out

def serialize_attendance(d):
    return {"emp_code":d["emp_code"],"date":d["date"],"status":d["status"],
      "punch_in":to_ms(d.get("punch_in")),"punch_out":to_ms(d.get("punch_out")),
      "work_hours":d.get("work_hours"),"late_minutes":int(d.get("late_minutes",0) or 0),
      "overtime_minutes":int(d.get("overtime_minutes",0) or 0),"half_day":bool(d.get("half_day",False)),
      "history":serialize_history(d.get("history",[]))}

def paged(page:int,page_size:int):
    if page < 1 or page_size < 1 or page_size > 100: raise HTTPException(422,"page must be >= 1 and page_size must be 1..100")
    return (page-1)*page_size

class EmployeeIn(BaseModel):
    model_config=ConfigDict(extra="ignore")
    emp_code:str=Field(pattern=r"^EMP\d{4,6}$")
    name:str=Field(min_length=1,max_length=100)
    email:str=Field(max_length=120,pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department:str=Field(min_length=1,max_length=50)
    shift_start:str=Field(default="09:30",pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    shift_end:str=Field(default="18:30",pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    joined_on:str
    @field_validator("joined_on")
    @classmethod
    def valid_join_date(cls,v):
        try:
            if date.fromisoformat(v).isoformat()!=v: raise ValueError()
        except Exception: raise ValueError("joined_on must be YYYY-MM-DD")
        return v
    @model_validator(mode="after")
    def shift_not_equal(self):
        if self.shift_start==self.shift_end: raise ValueError("shift_start must differ from shift_end")
        return self

class PunchInIn(BaseModel):
    model_config=ConfigDict(extra="ignore")
    emp_code:str
    punched_at:Optional[int]=None
    status:Literal["PRESENT","WFH","ON_DUTY"]="PRESENT"
    @field_validator("punched_at")
    @classmethod
    def valid_ts(cls,v):
        if v is not None and (type(v) is not int or not EPOCH_MIN<=v<=EPOCH_MAX): raise ValueError("punched_at must be epoch milliseconds")
        return v

class PunchOutIn(BaseModel):
    model_config=ConfigDict(extra="ignore")
    emp_code:str
    punched_at:Optional[int]=None
    @field_validator("punched_at")
    @classmethod
    def valid_ts(cls,v):
        if v is not None and (type(v) is not int or not EPOCH_MIN<=v<=EPOCH_MAX): raise ValueError("punched_at must be epoch milliseconds")
        return v

class RegularizeIn(BaseModel):
    model_config=ConfigDict(extra="ignore")
    status:Optional[Literal["PRESENT","WFH","ON_DUTY","ABSENT","LEAVE"]]=None
    punch_in:Optional[int]=None
    punch_out:Optional[int]=None
    reason:str=Field(min_length=5,max_length=200)
    regularized_by:str=Field(min_length=1,max_length=50)
    @field_validator("punch_in","punch_out")
    @classmethod
    def valid_ts(cls,v):
        if v is not None and (type(v) is not int or not EPOCH_MIN<=v<=EPOCH_MAX): raise ValueError("punch times must be epoch milliseconds")
        return v

# -------------------------------- endpoints ---------------------------------
@app.get("/health")
def health():
    try: db.command("ping"); return {"status":"ok"}
    except PyMongoError: raise HTTPException(503,"MongoDB unavailable")

@app.post("/employees",status_code=201)
def create_employee(body:EmployeeIn):
    doc=body.model_dump(); doc["created_at"]=datetime.now(UTC)
    try: db.employees.insert_one(doc)
    except DuplicateKeyError: raise HTTPException(409,"emp_code already exists")
    return {**doc,"created_at":to_ms(doc["created_at"])}

@app.get("/employees")
def list_employees(department:Optional[str]=None,page:int=Query(1,ge=1),page_size:int=Query(20,ge=1,le=100)):
    skip=paged(page,page_size); q={"department":department} if department is not None else {}
    total=db.employees.count_documents(q)
    items=list(db.employees.find(q,{"_id":0}).sort("emp_code",ASCENDING).skip(skip).limit(page_size))
    for x in items: x["created_at"]=to_ms(x.get("created_at"))
    return {"items":items,"total":total,"page":page,"page_size":page_size}

@app.post("/attendance/punch-in",status_code=201)
def punch_in(body:PunchInIn):
    emp=db.employees.find_one({"emp_code":body.emp_code})
    if not emp: raise HTTPException(404,"unknown employee")
    ts=epoch_dt(body.punched_at,default_now=True); day=attendance_day(ts,emp); ds=day.isoformat()
    derived=compute_derived(emp,day,ts,None)
    doc={"emp_code":body.emp_code,"date":ds,"status":body.status,"punch_in":ts,"punch_out":None,
         **derived,"history":[]}
    try: db.attendance_logs.insert_one(doc)
    except DuplicateKeyError: raise HTTPException(409,"already punched in for this date")
    return serialize_attendance(doc)

@app.post("/attendance/punch-out")
def punch_out(body:PunchOutIn):
    emp=db.employees.find_one({"emp_code":body.emp_code})
    if not emp: raise HTTPException(404,"unknown employee")
    ts=epoch_dt(body.punched_at,default_now=True)
    # Select the most recent punch-in at/before the requested instant, then atomically close it.
    record=db.attendance_logs.find_one({"emp_code":body.emp_code,"punch_in":{"$ne":None,"$lte":ts}},sort=[("punch_in",DESCENDING)])
    if not record:
        any_record=db.attendance_logs.find_one({"emp_code":body.emp_code,"punch_in":{"$ne":None}},sort=[("punch_in",DESCENDING)])
        if any_record: raise HTTPException(422,"punched_at must be after punch_in")
        raise HTTPException(404,"no punch-in found")
    if record.get("punch_out") is not None: raise HTTPException(409,"already punched out")
    pin=truncate_seconds(record["punch_in"])
    if ts<=pin or (ts-pin).total_seconds()>86400: raise HTTPException(422,"punch-out must be after punch-in and within 24 hours")
    derived=compute_derived(emp,date.fromisoformat(record["date"]),pin,ts)
    updated=db.attendance_logs.find_one_and_update({"_id":record["_id"],"punch_out":None},
       {"$set":{"punch_out":ts,**derived}},return_document=ReturnDocument.AFTER)
    if updated is None: raise HTTPException(409,"already punched out")
    return serialize_attendance(updated)

@app.get("/attendance")
def list_attendance(emp_code:Optional[str]=None,date_from:Optional[str]=None,date_to:Optional[str]=None,
                    status:Optional[Literal["PRESENT","ABSENT","LEAVE","WFH","ON_DUTY"]]=None,
                    page:int=Query(1,ge=1),page_size:int=Query(20,ge=1,le=100)):
    skip=paged(page,page_size)
    if date_from: parse_date(date_from,"date_from")
    if date_to: parse_date(date_to,"date_to")
    if date_from and date_to and date_from>date_to: raise HTTPException(422,"date_from must be <= date_to")
    q={}
    if emp_code: q["emp_code"]=emp_code
    if status: q["status"]=status
    if date_from or date_to:
        q["date"]={}
        if date_from:q["date"]["$gte"]=date_from
        if date_to:q["date"]["$lte"]=date_to
    total=db.attendance_logs.count_documents(q)
    docs=db.attendance_logs.find(q,{"_id":0}).sort([("date",DESCENDING),("emp_code",ASCENDING)]).skip(skip).limit(page_size)
    return {"items":[serialize_attendance(d) for d in docs],"total":total,"page":page,"page_size":page_size}

@app.patch("/attendance/{emp_code}/{date}")
def regularize(emp_code:str,date:str,body:RegularizeIn):
    day=parse_date(date)
    emp=db.employees.find_one({"emp_code":emp_code})
    if not emp: raise HTTPException(404,"unknown employee")
    record=db.attendance_logs.find_one({"emp_code":emp_code,"date":date})
    if not record: raise HTTPException(404,"attendance record not found")
    old={"status":record.get("status"),"punch_in":record.get("punch_in"),"punch_out":record.get("punch_out")}
    new=old.copy()
    supplied=body.model_fields_set
    if "status" in supplied and body.status is not None:new["status"]=body.status
    if "punch_in" in supplied:new["punch_in"]=epoch_dt(body.punch_in) if body.punch_in is not None else None
    if "punch_out" in supplied:new["punch_out"]=epoch_dt(body.punch_out) if body.punch_out is not None else None
    for k in ("punch_in","punch_out"):
        if new[k] is not None:new[k]=truncate_seconds(new[k])
    if new["status"] in {"ABSENT","LEAVE"}:
        if ("punch_in" in supplied and body.punch_in is not None) or ("punch_out" in supplied and body.punch_out is not None):
            raise HTTPException(422,"ABSENT and LEAVE cannot have supplied punch times")
        new["punch_in"]=new["punch_out"]=None
    else:
        if new["punch_in"] is None: raise HTTPException(422,"presence status requires punch_in")
        local=new["punch_in"].astimezone(IST)
        expected_day=attendance_day(new["punch_in"],emp)
        if expected_day!=day: raise HTTPException(422,"punch_in must belong to this attendance date")
        if new["punch_out"] is not None:
            delta=(new["punch_out"]-new["punch_in"]).total_seconds()
            if delta<=0 or delta>86400: raise HTTPException(422,"punch_out must be after punch_in and within 24 hours")
    derived=compute_derived(emp,day,new["punch_in"],new["punch_out"])
    final={**new,**derived}
    tracked=["status","punch_in","punch_out","work_hours","late_minutes","overtime_minutes","half_day"]
    changes={}
    for k in tracked:
        before=record.get(k,False if k=="half_day" else 0 if k in ("late_minutes","overtime_minutes") else None)
        after=final.get(k)
        if before!=after: changes[k]={"from":before,"to":after}
    if not changes: raise HTTPException(422,"request does not change the record")
    entry={"at":datetime.now(UTC),"by":body.regularized_by,"reason":body.reason,"changes":changes}
    # Optimistic concurrency: compare original fields; loser receives 409 rather than losing history.
    query={"_id":record["_id"],"status":record.get("status"),"punch_in":record.get("punch_in"),"punch_out":record.get("punch_out")}
    update={"$set":final,"$push":{"history":entry}}
    updated=db.attendance_logs.find_one_and_update(query,update,return_document=ReturnDocument.AFTER)
    if updated is None: raise HTTPException(409,"record changed concurrently; retry regularization")
    return serialize_attendance(updated)

# ----------------------------- aggregation utils ----------------------------
def month_match(month):
    first,last=parse_month(month); return first.isoformat(),last.isoformat()

def working_days(start:date,end:date):
    if start>end:return 0
    weeks,rem=divmod((end-start).days+1,7)
    return weeks*5+sum(1 for i in range(rem) if (start+timedelta(days=weeks*7+i)).weekday()<5)

def attendance_month_pipeline(emp_code,first,last):
    return [{"$match":{"emp_code":emp_code,"date":{"$gte":first,"$lte":last}}},
      {"$addFields":{"_weekday":{"$dayOfWeek":{"$dateFromString":{"dateString":"$date"}}}}},
      {"$group":{"_id":None,
       "present_days":{"$sum":{"$cond":[{"$and":[{"$in":["$status",list(PRESENCE)]},{"$in":["$_weekday",[2,3,4,5,6]]}]},{"$cond":[{"$eq":["$half_day",True]},0.5,1]},0]}},
       "leave_days":{"$sum":{"$cond":[{"$eq":["$status","LEAVE"]},1,0]}},
       "late_count":{"$sum":{"$cond":[{"$gt":[{"$ifNull":["$late_minutes",0]},0]},1,0]}},
       "total_late_minutes":{"$sum":{"$ifNull":["$late_minutes",0]}},"total_overtime_minutes":{"$sum":{"$ifNull":["$overtime_minutes",0]}}}}]

@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(emp_code:str,month:str):
    emp=db.employees.find_one({"emp_code":emp_code},{"_id":0,"joined_on":1})
    if not emp: raise HTTPException(404,"unknown employee")
    first,last=month_match(month); start=max(date.fromisoformat(emp["joined_on"]),date.fromisoformat(first)); end=date.fromisoformat(last)
    wd=working_days(start,end) if start<=end else 0
    result=list(db.attendance_logs.aggregate(attendance_month_pipeline(emp_code,first,last)))
    x=result[0] if result else {}
    present=round_half_up(x.get("present_days",0),2)
    pct=round_half_up(present/wd*100,4) if wd else None
    return {"emp_code":emp_code,"month":month,"working_days":wd,"present_days":present,"leave_days":int(x.get("leave_days",0)),
      "late_count":int(x.get("late_count",0)),"total_late_minutes":int(x.get("total_late_minutes",0)),
      "total_overtime_minutes":int(x.get("total_overtime_minutes",0)),"attendance_pct":pct}

@app.get("/analytics/departments/summary")
def department_summary(month:str,department:Optional[str]=None):
    first,last=month_match(month)
    pipeline=[{"$match":{"joined_on":{"$lte":last},**({"department":department} if department is not None else {})}},
      {"$lookup":{"from":"attendance_logs","let":{"code":"$emp_code"},"pipeline":[{"$match":{"$expr":{"$and":[{"$eq":["$emp_code","$$code"]},{"$gte":["$date",first]},{"$lte":["$date",last]}]}}}],"as":"logs"}},
      {"$unwind":{"path":"$logs","preserveNullAndEmptyArrays":True}},
      {"$addFields":{"_weekday":{"$cond":[{"$ne":[{"$ifNull":["$logs.date",None]},None]},{"$dayOfWeek":{"$dateFromString":{"dateString":"$logs.date"}}},None]}}},
      {"$group":{"_id":"$department","headcount":{"$addToSet":"$emp_code"},
       "present_days":{"$sum":{"$cond":[{"$and":[{"$in":["$logs.status",list(PRESENCE)]},{"$in":["$_weekday",[2,3,4,5,6]]}]},{"$cond":[{"$eq":["$logs.half_day",True]},0.5,1]},0]}},
       "_work_hours":{"$push":{"$cond":[{"$and":[{"$in":["$logs.status",list(PRESENCE)]},{"$ne":[{"$ifNull":["$logs.work_hours",None]},None]}]},"$logs.work_hours",None]}},
       "late_count":{"$sum":{"$cond":[{"$gt":[{"$ifNull":["$logs.late_minutes",0]},0]},1,0]}},
       "total_late_minutes":{"$sum":{"$ifNull":["$logs.late_minutes",0]}},
       "leave_count":{"$sum":{"$cond":[{"$eq":["$logs.status","LEAVE"]},1,0]}},
       "on_duty_count":{"$sum":{"$cond":[{"$eq":["$logs.status","ON_DUTY"]},1,0]}}}},
      {"$project":{"_id":0,"department":"$_id","headcount":{"$size":"$headcount"},"present_days":{"$round":["$present_days",2]},
       "avg_work_hours":{"$let":{"vars":{"vals":{"$filter":{"input":"$_work_hours","as":"v","cond":{"$ne":["$$v",None]}}}},"in":{"$cond":[{"$gt":[{"$size":"$$vals"},0]},{"$divide":[{"$floor":{"$add":[{"$multiply":[{"$avg":"$$vals"},100]},0.5]}},100]},None]}}},
       "late_count":1,"total_late_minutes":1,"leave_count":1,"on_duty_count":1}}, {"$sort":{"department":1}}]
    items=list(db.employees.aggregate(pipeline))
    return {"month":month,"items":items}

@app.get("/analytics/leaderboard/late")
def late_leaderboard(month:str,limit:int=Query(10,ge=1,le=50),department:Optional[str]=None):
    first,last=month_match(month)
    pipeline=[{"$match":{"date":{"$gte":first,"$lte":last},"late_minutes":{"$gt":0}}},
      {"$group":{"_id":"$emp_code","total_late_minutes":{"$sum":"$late_minutes"},"late_count":{"$sum":1}}},
      {"$lookup":{"from":"employees","localField":"_id","foreignField":"emp_code","as":"employee"}},
      {"$unwind":"$employee"}]
    if department is not None:pipeline.append({"$match":{"employee.department":department}})
    pipeline += [{"$setWindowFields":{"sortBy":{"total_late_minutes":-1},"output":{"rank":{"$rank":{}}}}},
      {"$match":{"rank":{"$lte":limit}}},{"$sort":{"total_late_minutes":-1,"_id":1}},
      {"$project":{"_id":0,"rank":1,"emp_code":"$_id","name":"$employee.name","department":"$employee.department","total_late_minutes":1,"late_count":1}}]
    return {"month":month,"items":list(db.attendance_logs.aggregate(pipeline))}

@app.get("/analytics/departments/{department}/trend")
def department_trend(department:str,from_:str=Query(...,alias="from"),to:str=Query(...)):
    start=parse_date(from_,"from"); end=parse_date(to,"to")
    if end<start: raise HTTPException(422,"to must be >= from")
    span=(end-start).days+1
    if span>92: raise HTTPException(422,"date range cannot exceed 92 days")
    if not db.employees.find_one({"department":department}): raise HTTPException(404,"unknown department")
    # MongoDB generates every requested calendar day using $range/$map; $lookup pulls logs and employee counts.
    pipeline=[{"$documents":[{"start":datetime.combine(start,time.min,UTC),"end":datetime.combine(end,time.min,UTC),"department":department,"days":span}]},
      {"$project":{"department":1,"dates":{"$map":{"input":{"$range":[0,"$days"]},"as":"n","in":{"$dateAdd":{"startDate":"$start","unit":"day","amount":"$$n"}}}}}},
      {"$unwind":"$dates"},{"$project":{"department":1,"day":"$dates","date":{"$dateToString":{"date":"$dates","format":"%Y-%m-%d","timezone":"UTC"}},"weekday":{"$dayOfWeek":"$dates"}}},
      {"$lookup":{"from":"employees","let":{"dep":"$department","d":"$date"},"pipeline":[{"$match":{"$expr":{"$and":[{"$eq":["$department","$$dep"]},{"$lte":["$joined_on","$$d"]}]}}},{"$count":"n"}],"as":"hc"}},
      {"$lookup":{"from":"attendance_logs","let":{"dep":"$department","d":"$date"},"pipeline":[{"$match":{"$expr":{"$eq":["$date","$$d"]}}},{"$lookup":{"from":"employees","localField":"emp_code","foreignField":"emp_code","as":"e"}},{"$unwind":"$e"},{"$match":{"$expr":{"$eq":["$e.department","$$dep"]}}},{"$group":{"_id":None,"present_count":{"$sum":{"$cond":[{"$in":["$status",list(PRESENCE)]},{"$cond":[{"$eq":["$half_day",True]},0.5,1]},0]}},"late_count":{"$sum":{"$cond":[{"$gt":[{"$ifNull":["$late_minutes",0]},0]},1,0]}}}}],"as":"daily"}},
      {"$addFields":{"headcount":{"$ifNull":[{"$arrayElemAt":["$hc.n",0]},0]},"present_count":{"$ifNull":[{"$arrayElemAt":["$daily.present_count",0]},0]},"late_count":{"$ifNull":[{"$arrayElemAt":["$daily.late_count",0]},0]},"is_working_day":{"$in":["$weekday",[2,3,4,5,6]]}}},
      {"$addFields":{"attendance_rate":{"$cond":[{"$and":["$is_working_day",{"$gt":["$headcount",0]}]},{"$divide":[{"$floor":{"$add":[{"$multiply":[{"$divide":["$present_count","$headcount"]},10000]},0.5]}},10000]},None]}}},
      {"$setWindowFields":{"sortBy":{"day":1},"output":{"_avg7":{"$avg":"$attendance_rate","window":{"documents":[-6,0]}}}}},
      {"$project":{"_id":0,"date":1,"is_working_day":1,"headcount":1,"present_count":{"$round":["$present_count",2]},"late_count":1,"attendance_rate":1,"moving_avg_7d":{"$cond":[{"$eq":[{"$type":"$_avg7"},"missing"]},None,{"$divide":[{"$floor":{"$add":[{"$multiply":["$_avg7",10000]},0.5]}},10000]}]}}},
      {"$sort":{"date":1}}]
    return {"department":department,"items":list(db.aggregate(pipeline))}

@app.get("/admin/explain/{endpoint}")
def explain_endpoint(endpoint:str,emp_code:Optional[str]=None,date_from:Optional[str]=None,date_to:Optional[str]=None,
                     status:Optional[str]=None,page:int=Query(1,ge=1),page_size:int=Query(20,ge=1,le=100),
                     month:Optional[str]=None,limit:int=Query(10,ge=1,le=50),department:Optional[str]=None,
                     from_:Optional[str]=Query(None,alias="from"),to:Optional[str]=None):
    # Explain the principal indexed operation with the endpoint's filters/sort. MongoDB explain is returned raw.
    if endpoint=="attendance_list":
        q={}
        if emp_code:q["emp_code"]=emp_code
        if status:q["status"]=status
        if date_from or date_to:
            q["date"]={}
            if date_from:q["date"]["$gte"]=date_from
            if date_to:q["date"]["$lte"]=date_to
        coll=db.attendance_logs; query={"find":coll.name,"filter":q,"sort":{"date":-1,"emp_code":1},"skip":(page-1)*page_size,"limit":page_size}
        explain=db.command("explain",query,verbosity="executionStats")
    elif endpoint=="employee_monthly":
        if not month or not emp_code:raise HTTPException(422,"employee_monthly requires emp_code and month")
        first,last=month_match(month); coll=db.attendance_logs
        explain=coll.database.command("explain",{"aggregate":coll.name,"pipeline":attendance_month_pipeline(emp_code,first,last),"cursor":{}},verbosity="executionStats")
    elif endpoint=="department_summary":
        if not month:raise HTTPException(422,"department_summary requires month")
        first,last=month_match(month); coll=db.employees
        pipeline=[{"$match":{"joined_on":{"$lte":last},**({"department":department} if department else {})}},{"$lookup":{"from":"attendance_logs","localField":"emp_code","foreignField":"emp_code","as":"logs"}}]
        explain=coll.database.command("explain",{"aggregate":coll.name,"pipeline":pipeline,"cursor":{}},verbosity="executionStats")
    elif endpoint=="late_leaderboard":
        if not month:raise HTTPException(422,"late_leaderboard requires month")
        first,last=month_match(month); coll=db.attendance_logs
        pipeline=[{"$match":{"date":{"$gte":first,"$lte":last},"late_minutes":{"$gt":0}}}]
        explain=coll.database.command("explain",{"aggregate":coll.name,"pipeline":pipeline,"cursor":{}},verbosity="executionStats")
    elif endpoint=="department_trend":
        trend_from=from_ or date_from; trend_to=to or date_to
        if not department or not trend_from or not trend_to:raise HTTPException(422,"department_trend requires department, from and to")
        parse_date(trend_from,"from");parse_date(trend_to,"to")
        if trend_to < trend_from: raise HTTPException(422,"to must be >= from")
        coll=db.attendance_logs
        explain=coll.database.command("explain",{"find":coll.name,"filter":{"date":{"$gte":trend_from,"$lte":trend_to}},"sort":{"date":1},"limit":100000},verbosity="executionStats")
    else: raise HTTPException(422,"endpoint must be attendance_list, employee_monthly, department_summary, late_leaderboard, or department_trend")
    return {"endpoint":endpoint,"collection":coll.name,"explain":json_util.loads(json_util.dumps(explain))}
