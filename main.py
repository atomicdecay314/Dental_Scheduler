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
from database import Base, engine, SessionLocal
from models import Doctor, Appointment
from schemas import ChatRequest, AppointmentRead, SlotOption, DoctorRead

# ---------------------------------------------------------------------------
# App setup
# ---------------------------------------------------------------------------

Base.metadata.create_all(bind=engine)

app = FastAPI(title="Dental Clinic Chatbot Scheduler")
app.mount("/static", StaticFiles(directory="static"), name="static")

@app.get("/", include_in_schema=False)
def root():
    return FileResponse("static/index.html")

gemini = genai.Client(api_key=os.environ["GEMINI_API_KEY"])

CLINIC_START = 9    # 9am
CLINIC_END   = 17   # 5pm
SLOT_MINUTES = 60

VALID_PROCEDURES = {
    "cleaning", "filling", "xray", "extraction",
    "implant", "braces_consultation", "retainer_fitting",
}

VALID_INTENTS = {"book", "list", "cancel", "greet", "unknown"}

# ---------------------------------------------------------------------------
# DB dependency
# ---------------------------------------------------------------------------

def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

# ---------------------------------------------------------------------------
# Gemini extraction
# ---------------------------------------------------------------------------

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

    # Sanitise — reject values that aren't in the allowed sets
    intent    = data.get("intent") if data.get("intent") in VALID_INTENTS else "unknown"
    procedure = data.get("procedure") if data.get("procedure") in VALID_PROCEDURES else None
    patient   = data.get("patient_name") if isinstance(data.get("patient_name"), str) else None

    start_time = None
    if data.get("datetime"):
        try:
            # Strip Z/timezone suffix — store as naive local datetime to avoid UTC shifts
            raw_dt = data["datetime"].rstrip("Z")
            if "+" in raw_dt[10:]:
                raw_dt = raw_dt[:raw_dt.index("+", 10)]
            dt = datetime.fromisoformat(raw_dt)
            # If no time component was given (midnight), treat as no time specified
            start_time = dt if (dt.hour != 0 or dt.minute != 0) else None
        except (ValueError, IndexError):
            pass

    return {"intent": intent, "procedure": procedure, "datetime": start_time, "patient_name": patient}


# ---------------------------------------------------------------------------
# Conversation state
# ---------------------------------------------------------------------------

CONFIRM_YES = re.compile(r"^(yes|confirm|ok|okay|sure|go ahead|book it|yep|y)$", re.IGNORECASE)
CONFIRM_NO  = re.compile(r"^(no|change|different|another|nope|n)$", re.IGNORECASE)


def fresh_state() -> dict:
    return {
        "patient_name":          None,
        "procedure":             None,
        "start_time":            None,
        "recommended_doctor_id": None,
        "recommended_room_id":   None,
        "awaiting_confirmation": False,
    }


# ---------------------------------------------------------------------------
# Chat endpoint
# ---------------------------------------------------------------------------

@app.post("/chat")
def chat(request: ChatRequest, db: Session = Depends(get_db)):
    session_id = request.session_id
    state = sessions.setdefault(session_id, fresh_state())
    msg   = request.message.strip()

    # --- Confirmation step ---
    if state["awaiting_confirmation"]:
        if CONFIRM_YES.match(msg):
            name      = state["patient_name"]
            procedure = state["procedure"]
            start     = state["start_time"]
            end       = start + timedelta(minutes=SLOT_MINUTES)
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

            doctor = db.get(__import__("models").Doctor, state["recommended_doctor_id"])
            room   = db.get(__import__("models").Room,   state["recommended_room_id"])
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

        return {"reply": "Please type 'confirm' to book the slot, or 'no' to choose a different time."}

    # --- Extract intent + entities via Gemini ---
    extracted = extract(msg)
    intent    = extracted["intent"]

    # --- Greeting ---
    if intent == "greet":
        return {"reply": "Hello! Ready to assist. You can book an appointment, check a patient's schedule, or view available slots."}

    # --- List appointments ---
    if intent == "list":
        name = extracted["patient_name"] or state.get("patient_name")
        if not name:
            return {"reply": "Which patient? Please provide their full name."}
        patient = db.query(__import__("models").Patient).filter_by(name=name).first()
        if not patient:
            return {"reply": f"No record found for '{name}'. Have they been registered?"}
        appts = scheduler.get_patient_appointments(db, patient.id)
        if not appts:
            return {"reply": f"No upcoming appointments found for {name}."}
        lines = [
            f"- {a.procedure.replace('_', ' ')} with {a.doctor.name} on {a.start_time.strftime('%A %d %B %Y at %I:%M %p')}"
            for a in appts
        ]
        return {"reply": f"Upcoming appointments for {name}:\n" + "\n".join(lines)}

    # --- Booking flow ---
    if intent == "book":
        procedure  = extracted["procedure"]  or state.get("procedure")
        start_time = extracted["datetime"]   or state.get("start_time")
        name       = extracted["patient_name"] or state.get("patient_name")

        if not procedure:
            return {"reply": "What procedure does the patient need? (e.g. cleaning, filling, extraction, xray, implant, braces consultation, retainer fitting)"}
        state["procedure"] = procedure

        if not start_time:
            return {"reply": f"What date and time for the {procedure.replace('_', ' ')}?"}
        state["start_time"] = start_time

        if not name:
            return {"reply": f"{procedure.replace('_', ' ')} on {start_time.strftime('%A %d %B at %I:%M %p')}. What is the patient's name?"}
        state["patient_name"] = name

        end_time = start_time + timedelta(minutes=SLOT_MINUTES)
        slot = scheduler.find_best_slot(db, procedure, start_time, end_time)
        if not slot:
            # Suggest the next available slot instead of just rejecting
            next_slot = scheduler.find_next_available_slot(db, procedure, start_time)
            if next_slot:
                next_doc, next_room, next_start = next_slot
                # Store the suggestion so "yes"/"confirm" triggers the booking
                state["recommended_doctor_id"] = next_doc.id
                state["recommended_room_id"]   = next_room.id
                state["start_time"]            = next_start
                state["awaiting_confirmation"] = True
                return {
                    "reply": (
                        f"No slot available for a {procedure.replace('_', ' ')} at {start_time.strftime('%I:%M %p on %A %d %B')}. "
                        f"Next available: {next_doc.name} in {next_room.name} on {next_start.strftime('%A %d %B at %I:%M %p')}. "
                        f"Type 'confirm' to book that or 'no' to try a different time."
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

    # --- Cancel (stub) ---
    if intent == "cancel":
        return {"reply": "Cancellations must be processed manually for now. This feature is coming soon."}

    return {"reply": "I didn't understand that. You can book an appointment, look up a patient's schedule, or check available slots via GET /slots."}


sessions: dict[str, dict] = {}

# ---------------------------------------------------------------------------
# Slots endpoint
# ---------------------------------------------------------------------------

@app.get("/slots", response_model=list[SlotOption])
def available_slots(procedure: str, date: str, db: Session = Depends(get_db)):
    try:
        day = datetime.strptime(date, "%Y-%m-%d")
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid date format. Use YYYY-MM-DD.")

    doctors = scheduler.get_doctors_for_procedure(db, procedure)
    rooms   = scheduler.get_rooms_for_procedure(db, procedure)

    if not doctors or not rooms:
        raise HTTPException(status_code=404, detail=f"No doctors or rooms found for procedure '{procedure}'.")

    options: list[SlotOption] = []
    slot_start = day.replace(hour=CLINIC_START, minute=0, second=0, microsecond=0)
    day_end    = day.replace(hour=CLINIC_END,   minute=0, second=0, microsecond=0)

    while slot_start + timedelta(minutes=SLOT_MINUTES) <= day_end:
        slot_end = slot_start + timedelta(minutes=SLOT_MINUTES)
        for doctor in doctors:
            if not scheduler.is_doctor_available(db, doctor.id, slot_start, slot_end):
                continue
            for room in rooms:
                if not scheduler.is_room_available(db, room.id, slot_start, slot_end):
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
        slot_start += timedelta(minutes=SLOT_MINUTES)

    options.sort(key=lambda s: s.idle_time_score)
    return options


# ---------------------------------------------------------------------------
# Admin endpoints
# ---------------------------------------------------------------------------

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
