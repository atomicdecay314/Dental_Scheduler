import json
import os
import re
from datetime import datetime, timedelta

from google import genai
from fastapi import FastAPI, Depends, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy.orm import Session

import scheduler
import waitlist as wl
from database import Base, engine, SessionLocal
from models import Doctor, Room, Patient, Appointment, ProcedureConfig, Waitlist
from schemas import (
    ChatRequest, AppointmentRead, SlotOption, DoctorRead,
    ProcedureConfigCreate, ProcedureConfigRead,
    WaitlistRead, TriggerWaitlistRequest,
)

Base.metadata.create_all(bind=engine)

app = FastAPI(title="Dental Clinic Chatbot Scheduler")
app.mount("/static", StaticFiles(directory="static"), name="static")

@app.get("/", include_in_schema=False)
def root():
    return FileResponse("static/index.html")

gemini = genai.Client(api_key=os.environ["GEMINI_API_KEY"])

ADMIN_KEY = os.environ.get("ADMIN_KEY", "")

CLINIC_START = 9    # 9am
CLINIC_END   = 17   # 5pm

VALID_PROCEDURES = {
    "cleaning", "filling", "xray", "extraction",
    "implant", "braces_consultation", "retainer_fitting",
}

VALID_INTENTS = {"book", "list", "cancel", "greet", "unknown"}

def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def require_admin(admin_key: str = ""):
    if not ADMIN_KEY or admin_key != ADMIN_KEY:
        raise HTTPException(status_code=403, detail="Invalid or missing admin key.")


EXTRACTION_PROMPT = """Extract from this dental clinic message. Return JSON only, no markdown.
Today: {today} ({weekday}).
Rules: datetime must be "YYYY-MM-DDThh:mm:ss" (no Z/timezone). If no time given, datetime=null. Use next future occurrence for weekday names.
{{"intent":"book|list|cancel|greet|unknown","procedure":"cleaning|filling|xray|extraction|implant|braces_consultation|retainer_fitting|null","datetime":"...|null","patient_name":"...|null"}}
Message: "{message}"
"""


def extract(message: str) -> dict:
    """Send message to Gemini and return structured extraction dict."""
    now = datetime.now()
    prompt = EXTRACTION_PROMPT.format(
        today=now.strftime("%Y-%m-%d"),
        weekday=now.strftime("%A"),
        message=message.replace('"', "'"),
    )
    try:
        response = gemini.models.generate_content(model="gemini-2.5-flash-lite", contents=prompt)
        raw = response.text.strip().removeprefix("```json").removesuffix("```").strip()
        data = json.loads(raw)
    except Exception as e:
        print(f"[Gemini error] {e}")
        return {"intent": "unknown", "procedure": None, "datetime": None, "patient_name": None}

    intent    = data.get("intent") if data.get("intent") in VALID_INTENTS else "unknown"
    procedure = data.get("procedure") if data.get("procedure") in VALID_PROCEDURES else None
    patient   = data.get("patient_name") if isinstance(data.get("patient_name"), str) else None

    start_time = None
    if data.get("datetime"):
        try:
            raw_dt = data["datetime"].rstrip("Z")
            if "+" in raw_dt[10:]:
                raw_dt = raw_dt[:raw_dt.index("+", 10)]
            dt = datetime.fromisoformat(raw_dt)
            start_time = dt if (dt.hour != 0 or dt.minute != 0) else None
        except (ValueError, IndexError):
            pass

    return {"intent": intent, "procedure": procedure, "datetime": start_time, "patient_name": patient}


CONFIRM_YES      = re.compile(r"^(yes|confirm|ok|okay|sure|go ahead|book it|yep|y)$", re.IGNORECASE)
CONFIRM_NO       = re.compile(r"^(no|change|different|another|nope|n)$", re.IGNORECASE)
CONFIRM_WAITLIST = re.compile(r"^waitlist$", re.IGNORECASE)
RE_EMAIL         = re.compile(r"\S+@\S+")
RE_PHONE         = re.compile(r"\d[\d\s\-\(\)]{7,}")


def fresh_state() -> dict:
    return {
        "patient_name": None,
        "procedure": None,
        "start_time": None,
        "original_start_time": None,
        "duration_minutes": None,
        "buffer_minutes": None,
        "recommended_doctor_id": None,
        "recommended_room_id": None,
        "awaiting_confirmation": False,
        "awaiting_contact": False,
        "pending_waitlist": False,
    }


@app.post("/chat")
def chat(request: ChatRequest, db: Session = Depends(get_db)):
    session_id = request.session_id
    state = sessions.setdefault(session_id, fresh_state())
    msg   = request.message.strip()

    # --- Contact info collection step ---
    just_handled_contact = False
    if state["awaiting_contact"]:
        email_match = RE_EMAIL.search(msg)
        phone_match = RE_PHONE.search(msg)
        if email_match or phone_match:
            patient = db.query(Patient).filter_by(name=state["patient_name"]).first()
            if patient:
                if email_match:
                    patient.email = email_match.group(0)
                if phone_match:
                    patient.phone = phone_match.group(0)
                db.commit()
            state["awaiting_contact"] = False
            just_handled_contact = True
        else:
            return {"reply": "Please provide a phone number or email address (e.g. 9820012345 or name@example.com)."}

    # --- Slot confirmation step ---
    if state["awaiting_confirmation"]:
        if CONFIRM_WAITLIST.match(msg):
            name      = state["patient_name"]
            procedure = state["procedure"]
            orig      = state.get("original_start_time") or state["start_time"]
            dur       = state.get("duration_minutes") or 60
            orig_end  = orig + timedelta(minutes=dur)
            patient   = scheduler.get_or_create_patient(db, name)
            entry     = wl.add_to_waitlist(
                db,
                patient_id=patient.id,
                procedure=procedure,
                requested_start=orig,
                requested_end=orig_end,
            )
            sessions[session_id] = fresh_state()
            sessions[session_id]["patient_name"] = name
            return {"reply": f"Added to waitlist at position {entry.priority}. We'll notify you if that slot opens up."}

        if CONFIRM_YES.match(msg):
            name      = state["patient_name"]
            procedure = state["procedure"]
            start     = state["start_time"]
            dur       = state.get("duration_minutes") or 60
            end       = start + timedelta(minutes=dur)
            patient   = scheduler.get_or_create_patient(db, name)
            try:
                appt = scheduler.book_appointment(
                    db,
                    patient_id=patient.id,
                    doctor_id=state["recommended_doctor_id"],
                    room_id=state["recommended_room_id"],
                    procedure=procedure,
                    start=start,
                    end=end,
                )
            except ValueError as e:
                state["awaiting_confirmation"] = False
                state["start_time"] = None
                return {"reply": f"That slot was just taken. Please try a different time. ({e})"}

            doctor = db.get(Doctor, state["recommended_doctor_id"])
            room   = db.get(Room,   state["recommended_room_id"])
            sessions[session_id] = fresh_state()
            sessions[session_id]["patient_name"] = name
            return {
                "reply": (
                    f"Booked. {name} — {procedure.replace('_', ' ')} with {doctor.name} "
                    f"in {room.name} on {appt.start_time.strftime('%A %d %B %Y at %I:%M %p')}."
                ),
                "booking_date": appt.start_time.strftime("%Y-%m-%d"),
            }

        if CONFIRM_NO.match(msg):
            state["awaiting_confirmation"] = False
            state["start_time"] = None
            return {"reply": "Understood. What date and time would the patient prefer?"}

        return {"reply": "Please type 'confirm' to book the slot, 'no' to choose a different time, or 'waitlist' to join the queue."}

    # After contact info is collected, synthesise a book extraction from existing state
    # instead of trying to parse the phone/email string as a new message.
    if just_handled_contact:
        extracted = {
            "intent": "book",
            "procedure": state.get("procedure"),
            "datetime": state.get("start_time"),
            "patient_name": state.get("patient_name"),
        }
        intent = "book"
    else:
        extracted = extract(msg)
        intent    = extracted["intent"]

    # --- Greeting (skip if mid-booking) ---
    if intent == "greet" and not state.get("procedure"):
        return {"reply": "Hello! Ready to assist. You can book an appointment, check a patient's schedule, or view available slots."}

    # --- List appointments ---
    if intent == "list":
        name = extracted["patient_name"] or state.get("patient_name")
        if not name:
            return {"reply": "Which patient? Please provide their full name."}
        patient = db.query(Patient).filter_by(name=name).first()
        if not patient:
            return {"reply": f"No record found for '{name}'. Have they been registered?"}
        appts = scheduler.get_patient_appointments(db, patient.id)
        if not appts:
            return {"reply": f"No upcoming appointments found for {name}."}
        lines = [
            f"- {a.procedure.replace('_', ' ')} with {a.doctor.name} on {a.start_time.strftime('%A %d %B %Y at %I:%M %p')} [{a.status}]"
            for a in appts
        ]
        return {"reply": f"Upcoming appointments for {name}:\n" + "\n".join(lines)}

    # --- Cancel intent ---
    if intent == "cancel":
        name      = extracted["patient_name"] or state.get("patient_name")
        procedure = extracted["procedure"]    or state.get("procedure")
        if not name:
            return {"reply": "Which patient's appointment should be cancelled? Please provide their full name."}
        patient = db.query(Patient).filter_by(name=name).first()
        if not patient:
            return {"reply": f"No record found for '{name}'."}
        query = db.query(Appointment).filter(
            Appointment.patient_id == patient.id,
            Appointment.status == "scheduled",
            Appointment.start_time >= datetime.now(),
        )
        if procedure:
            query = query.filter(Appointment.procedure == procedure)
        appt = query.order_by(Appointment.start_time).first()
        if not appt:
            desc = f" for {procedure.replace('_', ' ')}" if procedure else ""
            return {"reply": f"No upcoming scheduled appointment{desc} found for {name}."}
        appt.status = "cancelled"
        db.commit()
        wl.trigger_waitlist(db, appt.procedure, appt.start_time, appt.end_time)
        return {
            "reply": (
                f"Cancelled: {name}'s {appt.procedure.replace('_', ' ')} with {appt.doctor.name} "
                f"on {appt.start_time.strftime('%A %d %B %Y at %I:%M %p')}."
            )
        }

    # --- Booking flow ---
    if intent == "book" or state.get("procedure"):
        procedure  = extracted.get("procedure")  or state.get("procedure")
        start_time = extracted.get("datetime")   or state.get("start_time")
        name       = extracted.get("patient_name") or state.get("patient_name")

        if not procedure:
            return {"reply": "What procedure does the patient need? (e.g. cleaning, filling, extraction, xray, implant, braces consultation, retainer fitting)"}
        state["procedure"] = procedure

        # Resolve duration once procedure is confirmed
        if state.get("duration_minutes") is None:
            try:
                dur, buf_pct = scheduler.get_duration(db, procedure, None)
                state["duration_minutes"] = dur
                state["buffer_minutes"]   = scheduler.get_buffer_minutes(dur, buf_pct)
            except ValueError:
                return {"reply": f"Sorry, I don't have a duration config for '{procedure.replace('_', ' ')}'. Please contact the clinic."}

        if not start_time:
            return {"reply": f"What date and time for the {procedure.replace('_', ' ')}?"}
        state["start_time"] = start_time
        if state.get("original_start_time") is None:
            state["original_start_time"] = start_time

        if not name:
            return {"reply": f"{procedure.replace('_', ' ')} on {start_time.strftime('%A %d %B at %I:%M %p')}. What is the patient's name?"}
        state["patient_name"] = name

        # Contact info step — check before searching for slots
        if not state["awaiting_contact"]:
            patient = scheduler.get_or_create_patient(db, name)
            db.commit()  # persist patient now so the next request can find them
            if patient.phone is None and patient.email is None:
                state["awaiting_contact"] = True
                return {"reply": f"What's a phone number or email for {name} so we can send confirmations?"}

        dur = state["duration_minutes"]
        buf = state["buffer_minutes"]
        end_time = start_time + timedelta(minutes=dur)
        slot = scheduler.find_best_slot(db, procedure, start_time, end_time, buffer_minutes=buf)
        if not slot:
            next_slot = scheduler.find_next_available_slot(
                db, procedure, start_time,
                duration_minutes=dur,
                buffer_minutes=buf,
            )
            if next_slot:
                next_doc, next_room, next_start = next_slot
                state["recommended_doctor_id"] = next_doc.id
                state["recommended_room_id"]   = next_room.id
                state["start_time"]            = next_start
                state["awaiting_confirmation"] = True
                return {
                    "reply": (
                        f"No slot available for a {procedure.replace('_', ' ')} at {start_time.strftime('%I:%M %p on %A %d %B')}. "
                        f"Next available: {next_doc.name} in {next_room.name} on {next_start.strftime('%A %d %B at %I:%M %p')}. "
                        f"Type 'confirm' to book that, 'no' to try a different time, "
                        f"or 'waitlist' to be added to the queue for your original time."
                    )
                }
            state["start_time"] = None
            return {"reply": f"No available doctor or room for a {procedure.replace('_', ' ')} in the next 7 days. Please contact the clinic directly."}

        doctor, room = slot
        state["recommended_doctor_id"] = doctor.id
        state["recommended_room_id"]   = room.id
        state["awaiting_confirmation"] = True

        return {
            "reply": (
                f"Best available slot: {doctor.name} in {room.name} on "
                f"{start_time.strftime('%A %d %B %Y at %I:%M %p')} "
                f"(optimised for least idle time). Type 'confirm' to book or 'no' for a different time."
            )
        }

    return {"reply": "I didn't understand that. You can book an appointment, look up a patient's schedule, or cancel an existing appointment."}


sessions: dict[str, dict] = {}


# ---------------------------------------------------------------------------
# Public read endpoints
# ---------------------------------------------------------------------------

@app.get("/slots", response_model=list[SlotOption])
def available_slots(procedure: str, date: str, db: Session = Depends(get_db)):
    try:
        day = datetime.strptime(date, "%Y-%m-%d")
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid date format. Use YYYY-MM-DD.")

    try:
        duration_minutes, buf_pct = scheduler.get_duration(db, procedure, None)
    except ValueError:
        raise HTTPException(status_code=404, detail=f"No procedure config for '{procedure}'.")

    buffer_minutes = scheduler.get_buffer_minutes(duration_minutes, buf_pct)

    doctors = scheduler.get_doctors_for_procedure(db, procedure)
    rooms   = scheduler.get_rooms_for_procedure(db, procedure)

    if not doctors or not rooms:
        raise HTTPException(status_code=404, detail=f"No doctors or rooms found for procedure '{procedure}'.")

    options: list[SlotOption] = []
    slot_start = day.replace(hour=CLINIC_START, minute=0, second=0, microsecond=0)
    day_end    = day.replace(hour=CLINIC_END,   minute=0, second=0, microsecond=0)

    while slot_start + timedelta(minutes=duration_minutes) <= day_end:
        slot_end = slot_start + timedelta(minutes=duration_minutes)
        for doctor in doctors:
            if not scheduler.is_doctor_available(db, doctor.id, slot_start, slot_end, buffer_minutes):
                continue
            for room in rooms:
                if not scheduler.is_room_available(db, room.id, slot_start, slot_end, buffer_minutes):
                    continue
                score = scheduler.score_idle_time(db, doctor.id, room.id, slot_start, slot_end)
                options.append(SlotOption(
                    start_time=slot_start,
                    end_time=slot_end,
                    doctor=doctor.name,
                    doctor_id=doctor.id,
                    room=room.name,
                    room_id=room.id,
                    idle_time_score=round(score, 2),
                ))
        slot_start += timedelta(minutes=duration_minutes)

    options.sort(key=lambda s: s.idle_time_score)
    return options


@app.get("/doctors", response_model=list[DoctorRead])
def list_doctors(db: Session = Depends(get_db)):
    return db.query(Doctor).order_by(Doctor.id).all()


@app.get("/appointments", response_model=list[AppointmentRead])
def list_all_appointments(db: Session = Depends(get_db)):
    appts = scheduler.get_all_appointments(db)
    return [
        AppointmentRead(
            id=a.id,
            procedure=a.procedure,
            status=a.status,
            start_time=a.start_time,
            end_time=a.end_time,
            patient=a.patient,
            doctor=a.doctor,
            room=a.room,
        )
        for a in appts
    ]


@app.delete("/appointments")
def clear_all_appointments(db: Session = Depends(get_db)):
    db.query(Appointment).delete()
    db.commit()
    return {"message": "All appointments cleared."}


@app.post("/appointments/{appointment_id}/cancel", response_model=AppointmentRead)
def cancel_appointment(appointment_id: int, db: Session = Depends(get_db)):
    appt = db.get(Appointment, appointment_id)
    if not appt:
        raise HTTPException(status_code=404, detail="Appointment not found.")
    if appt.status == "cancelled":
        raise HTTPException(status_code=400, detail="Appointment is already cancelled.")
    appt.status = "cancelled"
    db.commit()
    wl.trigger_waitlist(db, appt.procedure, appt.start_time, appt.end_time)
    db.refresh(appt)
    return AppointmentRead(
        id=appt.id, procedure=appt.procedure, status=appt.status,
        start_time=appt.start_time, end_time=appt.end_time,
        patient=appt.patient, doctor=appt.doctor, room=appt.room,
    )


# ---------------------------------------------------------------------------
# Admin routes — require ?admin_key= matching ADMIN_KEY env var
# ---------------------------------------------------------------------------

@app.get("/admin", include_in_schema=False)
def admin_ui():
    return FileResponse("static/admin.html")


@app.get("/admin/procedures", response_model=list[ProcedureConfigRead])
def admin_list_procedures(admin_key: str = "", db: Session = Depends(get_db)):
    require_admin(admin_key)
    return db.query(ProcedureConfig).order_by(ProcedureConfig.procedure, ProcedureConfig.doctor_id).all()


@app.post("/admin/procedures", response_model=ProcedureConfigRead)
def admin_create_procedure(body: ProcedureConfigCreate, admin_key: str = "", db: Session = Depends(get_db)):
    require_admin(admin_key)
    row = ProcedureConfig(**body.model_dump())
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


@app.delete("/admin/procedures/{config_id}")
def admin_delete_procedure(config_id: int, admin_key: str = "", db: Session = Depends(get_db)):
    require_admin(admin_key)
    row = db.get(ProcedureConfig, config_id)
    if not row:
        raise HTTPException(status_code=404, detail="Procedure config not found.")
    db.delete(row)
    db.commit()
    return {"message": "Deleted."}


@app.get("/admin/waitlist", response_model=list[WaitlistRead])
def admin_list_waitlist(admin_key: str = "", db: Session = Depends(get_db)):
    require_admin(admin_key)
    entries = (
        db.query(Waitlist)
        .filter(Waitlist.notified == False)
        .order_by(Waitlist.requested_start, Waitlist.priority)
        .all()
    )
    return [
        WaitlistRead(
            id=e.id,
            patient_id=e.patient_id,
            patient_name=e.patient.name,
            procedure=e.procedure,
            doctor_id=e.doctor_id,
            requested_start=e.requested_start,
            requested_end=e.requested_end,
            priority=e.priority,
            notified=e.notified,
            created_at=e.created_at,
        )
        for e in entries
    ]


@app.post("/admin/waitlist/{entry_id}/promote")
def admin_promote_waitlist(entry_id: int, admin_key: str = "", db: Session = Depends(get_db)):
    require_admin(admin_key)
    entry = db.get(Waitlist, entry_id)
    if not entry:
        raise HTTPException(status_code=404, detail="Waitlist entry not found.")
    if entry.priority <= 1:
        return {"message": "Already at top priority."}
    # Swap with the entry above
    above = db.query(Waitlist).filter(
        Waitlist.procedure == entry.procedure,
        Waitlist.requested_start == entry.requested_start,
        Waitlist.requested_end == entry.requested_end,
        Waitlist.notified == False,
        Waitlist.priority == entry.priority - 1,
    ).first()
    if above:
        above.priority = entry.priority
    entry.priority -= 1
    db.commit()
    return {"message": f"Promoted to priority {entry.priority}."}


@app.delete("/admin/waitlist/{entry_id}")
def admin_delete_waitlist(entry_id: int, admin_key: str = "", db: Session = Depends(get_db)):
    require_admin(admin_key)
    entry = db.get(Waitlist, entry_id)
    if not entry:
        raise HTTPException(status_code=404, detail="Waitlist entry not found.")
    db.delete(entry)
    db.commit()
    return {"message": "Removed from waitlist."}


@app.post("/admin/waitlist/trigger")
def admin_trigger_waitlist(body: TriggerWaitlistRequest, admin_key: str = "", db: Session = Depends(get_db)):
    require_admin(admin_key)
    booked = wl.trigger_waitlist(db, body.procedure, body.freed_start, body.freed_end)
    return {"booked_count": len(booked), "appointment_ids": [a.id for a in booked]}
