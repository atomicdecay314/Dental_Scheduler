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
from models import Doctor, Room, Patient, Appointment, ProcedureConfig, Waitlist, DoctorAvailability, DoctorLeave
from schemas import (
    ChatRequest, AppointmentRead, SlotOption, DoctorRead,
    ProcedureConfigCreate, ProcedureConfigRead,
    WaitlistRead, TriggerWaitlistRequest,
    DoctorAvailabilityCreate, DoctorAvailabilityRead,
    DoctorLeaveCreate, DoctorLeaveRead,
    EarlyEndRequest,
)

Base.metadata.create_all(bind=engine)

# ---------------------------------------------------------------------------
# Incremental migrations — run at startup to patch existing DBs on Render
# ---------------------------------------------------------------------------

def _migrate():
    """Add any missing data that wasn't present when the DB was first seeded."""
    from models import doctor_procedures, room_procedures
    db = SessionLocal()
    try:
        palash = db.query(Doctor).filter(Doctor.name == "Dr. Palash Mehta").first()
        varun  = db.query(Doctor).filter(Doctor.name == "Dr. Varun Kumar").first()
        rhea   = db.query(Doctor).filter(Doctor.name == "Dr. Rhea Singh").first()
        room2  = db.query(Room).filter(Room.name == "Room 2").first()

        if not rhea or not room2:
            return

        # root_canal associations
        existing_dp = db.execute(
            doctor_procedures.select().where(
                doctor_procedures.c.doctor_id == rhea.id,
                doctor_procedures.c.procedure == "root_canal",
            )
        ).fetchone()
        if not existing_dp:
            db.execute(doctor_procedures.insert(), [{"doctor_id": rhea.id, "procedure": "root_canal"}])

        existing_rp = db.execute(
            room_procedures.select().where(
                room_procedures.c.room_id == room2.id,
                room_procedures.c.procedure == "root_canal",
            )
        ).fetchone()
        if not existing_rp:
            db.execute(room_procedures.insert(), [{"room_id": room2.id, "procedure": "root_canal"}])

        existing_cfg = db.query(ProcedureConfig).filter_by(procedure="root_canal", doctor_id=None).first()
        if not existing_cfg:
            db.add(ProcedureConfig(doctor_id=None, procedure="root_canal", duration_minutes=90, buffer_pct=10.0))

        # DoctorAvailability — seed if no rows exist yet
        if palash and varun and not db.query(DoctorAvailability).first():
            avails = []
            for day in range(5):           # Mon–Fri
                avails.append(DoctorAvailability(doctor_id=palash.id, day_of_week=day, start_time="09:00", end_time="17:00"))
            for day in [0, 2, 4]:          # Mon/Wed/Fri
                avails.append(DoctorAvailability(doctor_id=varun.id, day_of_week=day, start_time="09:00", end_time="17:00"))
            for day in [1, 3, 5]:          # Tue/Thu/Sat
                avails.append(DoctorAvailability(doctor_id=rhea.id, day_of_week=day, start_time="10:00", end_time="16:00"))
            db.add_all(avails)

        db.commit()
    except Exception:
        db.rollback()
    finally:
        db.close()

    # New nullable columns on appointments
    import sqlalchemy as sa
    with engine.connect() as conn:
        for col, typedef in [("actual_end_time", "DATETIME"), ("completed_at", "DATETIME")]:
            try:
                conn.execute(sa.text(f"ALTER TABLE appointments ADD COLUMN {col} {typedef}"))
                conn.commit()
            except Exception:
                pass  # column already exists


_migrate()

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
    "implant", "braces_consultation", "retainer_fitting", "root_canal",
}

VALID_INTENTS = {"book", "list", "cancel", "reschedule", "early_end", "greet", "unknown"}

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
Use intent "reschedule" when the message contains move/change/reschedule an existing appointment; datetime should be the new requested time.
Use intent "early_end" when the message indicates an appointment finished early (e.g. "Rahul's cleaning ended at 10:40", "appointment finished early", "end early for [name]"); datetime should be the actual finish time if mentioned.
{{"intent":"book|list|cancel|reschedule|early_end|greet|unknown","procedure":"cleaning|filling|xray|extraction|implant|braces_consultation|retainer_fitting|root_canal|null","datetime":"...|null","patient_name":"...|null"}}
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
            if dt.hour == 0 and dt.minute == 0:
                dt = None
            elif dt.date() < datetime.now().date():
                # Gemini sometimes returns a past weekday — advance by weeks until date is today or later
                while dt.date() < datetime.now().date():
                    dt += timedelta(weeks=1)
            start_time = dt
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
        # Rescheduling state
        "is_rescheduling": False,
        "rescheduling_appt_id": None,
        "old_start": None,
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
        is_reschedule = state.get("is_rescheduling", False)

        if not is_reschedule and CONFIRM_WAITLIST.match(msg):
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

            # For reschedule: cancel the old appointment first
            if is_reschedule and state.get("rescheduling_appt_id"):
                old_appt = db.get(Appointment, state["rescheduling_appt_id"])
                if old_appt and old_appt.status == "scheduled":
                    old_appt.status = "cancelled"
                    db.commit()
                    wl.trigger_waitlist(db, old_appt.procedure, old_appt.start_time, old_appt.end_time)

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

            doctor    = db.get(Doctor, state["recommended_doctor_id"])
            room      = db.get(Room,   state["recommended_room_id"])
            old_start = state.get("old_start")
            sessions[session_id] = fresh_state()
            sessions[session_id]["patient_name"] = name
            if is_reschedule and old_start:
                return {
                    "reply": (
                        f"Rescheduled. {name}'s {procedure.replace('_', ' ')} moved from "
                        f"{old_start.strftime('%A %d %B at %I:%M %p')} to "
                        f"{appt.start_time.strftime('%A %d %B %Y at %I:%M %p')} "
                        f"with {doctor.name} in {room.name}."
                    ),
                    "booking_date": appt.start_time.strftime("%Y-%m-%d"),
                }
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
            return {"reply": "Understood. What's the new date and time you'd like?" if is_reschedule else "Understood. What date and time would the patient prefer?"}

        prompt = "Please type 'confirm' to confirm the reschedule, or 'no' to try a different time." if is_reschedule \
            else "Please type 'confirm' to book the slot, 'no' to choose a different time, or 'waitlist' to join the queue."
        return {"reply": prompt}

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

    # --- Reschedule intent ---
    # Don't trigger if we're already mid-booking (Gemini can mis-classify a bare time like
    # "monday at 10am" as reschedule when the user is just answering "what time do you want?")
    _mid_booking = state.get("procedure") and not state.get("is_rescheduling")
    if (intent == "reschedule" or state.get("is_rescheduling")) and not _mid_booking:
        procedure  = extracted.get("procedure")    or state.get("procedure")
        name       = extracted.get("patient_name") or state.get("patient_name")
        new_time   = extracted.get("datetime")     or (state.get("start_time") if state.get("is_rescheduling") else None)

        if not name:
            return {"reply": "Which patient needs to reschedule? Please provide their full name."}
        state["patient_name"] = name

        if not procedure:
            return {"reply": "Which procedure needs to be rescheduled? (e.g. cleaning, filling, root canal)"}
        state["procedure"] = procedure

        # Find the appointment to reschedule (once)
        if not state.get("rescheduling_appt_id"):
            patient = db.query(Patient).filter_by(name=name).first()
            if not patient:
                return {"reply": f"No record found for '{name}'."}
            appt_to_move = (
                db.query(Appointment)
                .filter(
                    Appointment.patient_id == patient.id,
                    Appointment.status == "scheduled",
                    Appointment.start_time >= datetime.now(),
                    Appointment.procedure == procedure,
                )
                .order_by(Appointment.start_time)
                .first()
            )
            if not appt_to_move:
                return {"reply": f"No upcoming {procedure.replace('_', ' ')} appointment found for {name}."}
            state["rescheduling_appt_id"] = appt_to_move.id
            state["old_start"]            = appt_to_move.start_time
            state["is_rescheduling"]      = True

        if state.get("duration_minutes") is None:
            try:
                dur, buf_pct = scheduler.get_duration(db, procedure, None)
                state["duration_minutes"] = dur
                state["buffer_minutes"]   = scheduler.get_buffer_minutes(dur, buf_pct)
            except ValueError:
                return {"reply": f"Sorry, I don't have a duration config for '{procedure.replace('_', ' ')}'."}

        if not new_time:
            old_start = state["old_start"]
            return {"reply": f"What's the new date and time for {name}'s {procedure.replace('_', ' ')} (currently {old_start.strftime('%A %d %B at %I:%M %p')})?"}
        state["start_time"] = new_time
        if state.get("original_start_time") is None:
            state["original_start_time"] = new_time

        dur = state["duration_minutes"]
        buf = state["buffer_minutes"]
        end_time = new_time + timedelta(minutes=dur)
        old_start = state["old_start"]

        slot = scheduler.find_best_slot(db, procedure, new_time, end_time, buffer_minutes=buf)
        if not slot:
            next_slot = scheduler.find_next_available_slot(
                db, procedure, new_time,
                duration_minutes=dur, buffer_minutes=buf,
            )
            if next_slot:
                next_doc, next_room, next_start = next_slot
                state["recommended_doctor_id"] = next_doc.id
                state["recommended_room_id"]   = next_room.id
                state["start_time"]            = next_start
                state["awaiting_confirmation"] = True
                return {
                    "reply": (
                        f"No slot available at {new_time.strftime('%I:%M %p on %A %d %B')}. "
                        f"Next available: {next_doc.name} in {next_room.name} on {next_start.strftime('%A %d %B at %I:%M %p')}. "
                        f"Move {name}'s {procedure.replace('_', ' ')} from {old_start.strftime('%A %d %B at %I:%M %p')} to this slot? "
                        f"Type 'confirm' or 'no'."
                    )
                }
            state["start_time"] = None
            return {"reply": f"No available slot found for a {procedure.replace('_', ' ')} in the next 7 days. Please try a different time."}

        doctor, room = slot
        state["recommended_doctor_id"] = doctor.id
        state["recommended_room_id"]   = room.id
        state["awaiting_confirmation"] = True
        return {
            "reply": (
                f"Move {name}'s {procedure.replace('_', ' ')} from {old_start.strftime('%A %d %B at %I:%M %p')} "
                f"to {new_time.strftime('%A %d %B %Y at %I:%M %p')} with {doctor.name} in {room.name}? "
                f"Type 'confirm' or 'no'."
            )
        }

    # --- Early end intent ---
    if intent == "early_end" and not state.get("procedure"):
        name       = extracted.get("patient_name")
        procedure  = extracted.get("procedure")
        actual_end = extracted.get("datetime")

        if not name:
            return {"reply": "Which patient's appointment ended early? Please include their name, procedure, and the finish time."}
        if not procedure:
            return {"reply": f"Which procedure ended early for {name}? Also mention the finish time."}
        if not actual_end:
            return {"reply": f"What time did {name}'s {procedure.replace('_', ' ')} actually finish?"}

        patient = db.query(Patient).filter_by(name=name).first()
        if not patient:
            return {"reply": f"No record found for '{name}'."}

        appt = (
            db.query(Appointment)
            .filter(
                Appointment.patient_id == patient.id,
                Appointment.status == "scheduled",
                Appointment.procedure == procedure,
                Appointment.start_time <= datetime.now(),
            )
            .order_by(Appointment.start_time.desc())
            .first()
        )
        if not appt:
            return {"reply": f"No active appointment found for {name}'s {procedure.replace('_', ' ')}."}

        if actual_end >= appt.end_time:
            return {"reply": f"The finish time ({actual_end.strftime('%I:%M %p')}) is at or after the scheduled end ({appt.end_time.strftime('%I:%M %p')}). Please check the time."}
        if actual_end <= appt.start_time:
            return {"reply": f"The finish time must be after the appointment start ({appt.start_time.strftime('%I:%M %p')})."}

        appt.actual_end_time = actual_end
        appt.status          = "completed_early"
        appt.completed_at    = datetime.now()
        db.commit()

        gap     = scheduler.compute_freed_gap(db, appt)
        min_dur = scheduler.get_minimum_procedure_duration(db)

        if gap >= min_dur:
            wl.trigger_waitlist(db, appt.procedure, actual_end, appt.end_time)
            return {"reply": f"Got it — {name}'s {procedure.replace('_', ' ')} marked as finished at {actual_end.strftime('%I:%M %p')}. {gap} minutes freed up and waitlist has been notified."}
        return {"reply": f"Got it — {name}'s {procedure.replace('_', ' ')} marked as finished at {actual_end.strftime('%I:%M %p')}. The remaining {gap} minutes is too short to rebook (minimum is {min_dur} min)."}

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
            return {"reply": "What procedure does the patient need? (e.g. cleaning, filling, extraction, xray, implant, braces consultation, retainer fitting, root canal)"}
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
            # Check if all qualified doctors are off that specific day of week
            _DAY_NAMES = ['Monday','Tuesday','Wednesday','Thursday','Friday','Saturday','Sunday']
            qualified_docs = scheduler.get_doctors_for_procedure(db, procedure)
            requested_weekday = start_time.weekday()
            all_off_today = qualified_docs and all(
                not scheduler.is_doctor_on_duty(db, d.id, start_time, end_time)
                for d in qualified_docs
            )
            if all_off_today:
                schedules = []
                for doc in qualified_docs:
                    avail_rows = (
                        db.query(DoctorAvailability)
                        .filter(DoctorAvailability.doctor_id == doc.id)
                        .order_by(DoctorAvailability.day_of_week)
                        .all()
                    )
                    if avail_rows:
                        days = "/".join(_DAY_NAMES[a.day_of_week][:3] for a in avail_rows)
                        schedules.append(f"{doc.name} works {days}")
                msg = f"No doctors are available for {procedure.replace('_', ' ')} on {_DAY_NAMES[requested_weekday]}."
                if schedules:
                    msg += " " + ", ".join(schedules) + "."
                state["start_time"] = None
                return {"reply": msg}

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


def _appointment_read(a: Appointment, db: Session) -> AppointmentRead:
    try:
        dur, buf_pct = scheduler.get_duration(db, a.procedure, a.doctor_id)
        buf = scheduler.get_buffer_minutes(dur, buf_pct)
    except ValueError:
        buf = 0
    return AppointmentRead(
        id=a.id,
        procedure=a.procedure,
        status=a.status,
        start_time=a.start_time,
        end_time=a.end_time,
        actual_end_time=a.actual_end_time,
        completed_at=a.completed_at,
        buffer_minutes=buf,
        patient=a.patient,
        doctor=a.doctor,
        room=a.room,
    )


@app.get("/appointments", response_model=list[AppointmentRead])
def list_all_appointments(db: Session = Depends(get_db)):
    return [_appointment_read(a, db) for a in scheduler.get_all_appointments(db)]


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
    return _appointment_read(appt, db)


@app.post("/appointments/{appointment_id}/end-early")
def end_appointment_early(appointment_id: int, body: EarlyEndRequest, db: Session = Depends(get_db)):
    appt = db.get(Appointment, appointment_id)
    if not appt:
        raise HTTPException(status_code=404, detail="Appointment not found.")
    if appt.status != "scheduled":
        raise HTTPException(status_code=400, detail="Appointment is not currently scheduled.")
    if body.actual_end_time >= appt.end_time:
        raise HTTPException(status_code=400, detail="actual_end_time must be before the scheduled end_time.")
    if body.actual_end_time <= appt.start_time:
        raise HTTPException(status_code=400, detail="actual_end_time must be after start_time.")

    appt.actual_end_time = body.actual_end_time
    appt.status          = "completed_early"
    appt.completed_at    = datetime.now()
    db.commit()

    gap     = scheduler.compute_freed_gap(db, appt)
    min_dur = scheduler.get_minimum_procedure_duration(db)

    if gap >= min_dur:
        wl.trigger_waitlist(db, appt.procedure, body.actual_end_time, appt.end_time)
        return {"status": "completed_early", "freed_minutes": gap, "waitlist_triggered": True}
    return {
        "status": "completed_early",
        "freed_minutes": gap,
        "waitlist_triggered": False,
        "reason": f"Gap of {gap} min is less than shortest procedure ({min_dur} min) — slot not reopened.",
    }


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


# ---------------------------------------------------------------------------
# Admin — Doctor Availability
# ---------------------------------------------------------------------------

@app.get("/admin/availability", response_model=list[DoctorAvailabilityRead])
def admin_list_availability(admin_key: str = "", db: Session = Depends(get_db)):
    require_admin(admin_key)
    rows = db.query(DoctorAvailability).order_by(DoctorAvailability.doctor_id, DoctorAvailability.day_of_week).all()
    return [
        DoctorAvailabilityRead(
            id=r.id,
            doctor_id=r.doctor_id,
            doctor_name=r.doctor.name,
            day_of_week=r.day_of_week,
            start_time=r.start_time,
            end_time=r.end_time,
        )
        for r in rows
    ]


@app.post("/admin/availability", response_model=DoctorAvailabilityRead)
def admin_create_availability(body: DoctorAvailabilityCreate, admin_key: str = "", db: Session = Depends(get_db)):
    require_admin(admin_key)
    row = DoctorAvailability(**body.model_dump())
    db.add(row)
    try:
        db.commit()
    except Exception:
        db.rollback()
        raise HTTPException(status_code=400, detail="A schedule for this doctor/day already exists.")
    db.refresh(row)
    return DoctorAvailabilityRead(
        id=row.id, doctor_id=row.doctor_id, doctor_name=row.doctor.name,
        day_of_week=row.day_of_week, start_time=row.start_time, end_time=row.end_time,
    )


@app.delete("/admin/availability/{avail_id}")
def admin_delete_availability(avail_id: int, admin_key: str = "", db: Session = Depends(get_db)):
    require_admin(admin_key)
    row = db.get(DoctorAvailability, avail_id)
    if not row:
        raise HTTPException(status_code=404, detail="Availability row not found.")
    db.delete(row)
    db.commit()
    return {"message": "Deleted."}


# ---------------------------------------------------------------------------
# Admin — Doctor Leave
# ---------------------------------------------------------------------------

@app.get("/admin/leave", response_model=list[DoctorLeaveRead])
def admin_list_leave(admin_key: str = "", db: Session = Depends(get_db)):
    require_admin(admin_key)
    rows = db.query(DoctorLeave).order_by(DoctorLeave.doctor_id, DoctorLeave.date_from).all()
    return [
        DoctorLeaveRead(
            id=r.id, doctor_id=r.doctor_id, doctor_name=r.doctor.name,
            date_from=r.date_from, date_to=r.date_to, reason=r.reason,
        )
        for r in rows
    ]


@app.post("/admin/leave", response_model=DoctorLeaveRead)
def admin_create_leave(body: DoctorLeaveCreate, admin_key: str = "", db: Session = Depends(get_db)):
    require_admin(admin_key)
    row = DoctorLeave(**body.model_dump())
    db.add(row)
    db.commit()
    db.refresh(row)
    return DoctorLeaveRead(
        id=row.id, doctor_id=row.doctor_id, doctor_name=row.doctor.name,
        date_from=row.date_from, date_to=row.date_to, reason=row.reason,
    )


@app.delete("/admin/leave/{leave_id}")
def admin_delete_leave(leave_id: int, admin_key: str = "", db: Session = Depends(get_db)):
    require_admin(admin_key)
    row = db.get(DoctorLeave, leave_id)
    if not row:
        raise HTTPException(status_code=404, detail="Leave record not found.")
    db.delete(row)
    db.commit()
    return {"message": "Deleted."}
