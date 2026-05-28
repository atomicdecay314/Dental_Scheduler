import math
from datetime import datetime, timedelta, time as _time
from sqlalchemy.orm import Session
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from models import (
    Doctor, Room, Patient, Appointment, ProcedureConfig,
    DoctorAvailability, DoctorLeave,
    doctor_procedures, room_procedures,
)


# ---------------------------------------------------------------------------
# Lookup helpers — used by the chatbot to answer availability questions
# ---------------------------------------------------------------------------

def get_doctors_for_procedure(db: Session, procedure: str) -> list[Doctor]:
    """Return all doctors qualified to perform the given procedure."""
    qualified_ids = db.execute(
        select(doctor_procedures.c.doctor_id).where(
            doctor_procedures.c.procedure == procedure
        )
    ).scalars().all()
    return db.query(Doctor).filter(Doctor.id.in_(qualified_ids)).all()


def get_rooms_for_procedure(db: Session, procedure: str) -> list[Room]:
    """Return all rooms equipped for the given procedure."""
    equipped_ids = db.execute(
        select(room_procedures.c.room_id).where(
            room_procedures.c.procedure == procedure
        )
    ).scalars().all()
    return db.query(Room).filter(Room.id.in_(equipped_ids)).all()


def get_duration(db: Session, procedure: str, doctor_id: int | None) -> tuple[int, float]:
    """
    Return (duration_minutes, buffer_pct) for a procedure.
    Checks doctor-specific config first, then global fallback (doctor_id=NULL).
    Raises ValueError if no config exists for the procedure.
    """
    if doctor_id is not None:
        row = db.query(ProcedureConfig).filter(
            ProcedureConfig.procedure == procedure,
            ProcedureConfig.doctor_id == doctor_id,
        ).first()
        if row:
            return row.duration_minutes, row.buffer_pct

    row = db.query(ProcedureConfig).filter(
        ProcedureConfig.procedure == procedure,
        ProcedureConfig.doctor_id.is_(None),
    ).first()
    if row:
        return row.duration_minutes, row.buffer_pct

    raise ValueError(f"No procedure config for {procedure}")


def get_buffer_minutes(duration_minutes: int, buffer_pct: float) -> int:
    return math.ceil(duration_minutes * buffer_pct / 100)


def get_or_create_patient(db: Session, name: str, phone: str | None = None, email: str | None = None) -> Patient:
    """Return an existing patient matched by name, or create a new one."""
    patient = db.query(Patient).filter(Patient.name == name).first()
    if not patient:
        patient = Patient(name=name, phone=phone, email=email)
        db.add(patient)
        db.flush()  # get the id without committing
    return patient


# ---------------------------------------------------------------------------
# Availability check — core double-booking logic
# ---------------------------------------------------------------------------

def is_doctor_on_duty(db: Session, doctor_id: int, start: datetime, end: datetime) -> bool:
    """True if a DoctorAvailability row covers start's weekday and start/end times fall within it.
    Returns True when no availability rows exist at all (backward-compat with unseeded DBs)."""
    avail = db.query(DoctorAvailability).filter(
        DoctorAvailability.doctor_id == doctor_id,
        DoctorAvailability.day_of_week == start.weekday(),
    ).first()
    if avail is None:
        # If there are *any* rows for this doctor, the absence means day off.
        # If there are no rows at all (unseeded), allow the slot.
        has_any = db.query(DoctorAvailability).filter(
            DoctorAvailability.doctor_id == doctor_id
        ).first()
        return has_any is None
    avail_start = _time(*map(int, avail.start_time.split(":")))
    avail_end   = _time(*map(int, avail.end_time.split(":")))
    return start.time() >= avail_start and end.time() <= avail_end


def is_doctor_on_leave(db: Session, doctor_id: int, start: datetime, end: datetime) -> bool:
    """True if any leave record fully covers the start–end window."""
    leave = db.query(DoctorLeave).filter(
        DoctorLeave.doctor_id == doctor_id,
        DoctorLeave.date_from <= start,
        DoctorLeave.date_to   >= end,
    ).first()
    return leave is not None


def is_doctor_available(db: Session, doctor_id: int, start: datetime, end: datetime, buffer_minutes: int = 0) -> bool:
    """True if the doctor is on duty, not on leave, and has no conflicting appointment."""
    if not is_doctor_on_duty(db, doctor_id, start, end):
        return False
    if is_doctor_on_leave(db, doctor_id, start, end):
        return False
    effective_start = start - timedelta(minutes=buffer_minutes)
    conflict = db.query(Appointment).filter(
        Appointment.doctor_id == doctor_id,
        Appointment.status != "cancelled",
        Appointment.start_time < end,
        Appointment.end_time > effective_start,
    ).first()
    return conflict is None


def is_room_available(db: Session, room_id: int, start: datetime, end: datetime, buffer_minutes: int = 0) -> bool:
    """True if the room has no appointment overlapping the requested window (respecting buffer)."""
    effective_start = start - timedelta(minutes=buffer_minutes)
    conflict = db.query(Appointment).filter(
        Appointment.room_id == room_id,
        Appointment.status != "cancelled",
        Appointment.start_time < end,
        Appointment.end_time > effective_start,
    ).first()
    return conflict is None


def score_idle_time(db: Session, doctor_id: int, room_id: int, start: datetime, end: datetime) -> float:
    """
    Lower score = less idle time = better fit.

    Sums the gaps between the proposed slot and the nearest existing appointments
    before and after it, for both the doctor and room. A slot that tightly fills
    a gap scores lower than one that creates a long empty stretch.
    If no existing appointments exist for a resource, that gap is treated as a
    large number so we prefer filling around already-busy doctors/rooms.
    """
    def gap_score(filter_before: list, filter_after: list) -> float:
        prev = (
            db.query(Appointment)
            .filter(*filter_before)
            .order_by(Appointment.end_time.desc())
            .first()
        )
        nxt = (
            db.query(Appointment)
            .filter(*filter_after)
            .order_by(Appointment.start_time)
            .first()
        )
        before = (start - prev.end_time).total_seconds() if prev else 86_400
        after  = (nxt.start_time - end).total_seconds()  if nxt  else 86_400
        return before + after

    doctor_score = gap_score(
        [Appointment.doctor_id == doctor_id, Appointment.end_time <= start],
        [Appointment.doctor_id == doctor_id, Appointment.start_time >= end],
    )
    room_score = gap_score(
        [Appointment.room_id == room_id, Appointment.end_time <= start],
        [Appointment.room_id == room_id, Appointment.start_time >= end],
    )
    return doctor_score + room_score


def find_best_slot(
    db: Session,
    procedure: str,
    start: datetime,
    end: datetime,
    buffer_minutes: int = 0,
) -> tuple[Doctor, Room] | None:
    """
    Return the available (doctor, room) pair that minimises idle time around
    the proposed slot. Returns None if nothing is available.
    """
    doctors = get_doctors_for_procedure(db, procedure)
    rooms   = get_rooms_for_procedure(db, procedure)

    best: tuple[Doctor, Room] | None = None
    best_score = float("inf")

    for doctor in doctors:
        if not is_doctor_available(db, doctor.id, start, end, buffer_minutes):
            continue
        for room in rooms:
            if not is_room_available(db, room.id, start, end, buffer_minutes):
                continue
            score = score_idle_time(db, doctor.id, room.id, start, end)
            if score < best_score:
                best_score = score
                best = (doctor, room)

    return best


def find_next_available_slot(
    db: Session,
    procedure: str,
    from_time: datetime,
    duration_minutes: int,
    buffer_minutes: int = 0,
    clinic_start: int = 9,
    clinic_end: int = 17,
    max_days: int = 7,
) -> tuple[Doctor, Room, datetime] | None:
    """
    Scan forward from from_time to find the next available (doctor, room, start).
    Uses min(duration, 30)-minute steps plus appointment end-times as candidate
    starts so gaps between back-to-back appointments are never missed.
    Looks up to max_days ahead. Returns None if nothing found.
    """
    deadline = from_time + timedelta(days=max_days)
    step = timedelta(minutes=min(duration_minutes, 30))

    # Collect scan-based candidates starting from next step after from_time
    base = from_time.replace(second=0, microsecond=0)
    candidates: set[datetime] = set()
    t = base + step
    while t < deadline:
        if clinic_start <= t.hour < clinic_end:
            candidates.add(t)
        t += step

    # Add appointment end-time + buffer candidates for relevant doctors
    doctor_ids = [d.id for d in get_doctors_for_procedure(db, procedure)]
    if doctor_ids:
        existing = db.query(Appointment).filter(
            Appointment.doctor_id.in_(doctor_ids),
            Appointment.status != "cancelled",
            Appointment.end_time >= from_time,
            Appointment.end_time <= deadline,
        ).all()
        for appt in existing:
            candidate = appt.end_time + timedelta(minutes=buffer_minutes)
            if clinic_start <= candidate.hour < clinic_end:
                candidates.add(candidate)

    # Determine which days of week at least one qualified doctor is on duty.
    # If no DoctorAvailability rows exist at all, allow all days (unseeded DB).
    duty_weekdays: set[int] | None = None
    if doctor_ids:
        avail_rows = db.query(DoctorAvailability).filter(
            DoctorAvailability.doctor_id.in_(doctor_ids)
        ).all()
        if avail_rows:
            duty_weekdays = {r.day_of_week for r in avail_rows}

    if duty_weekdays is not None:
        candidates = {c for c in candidates if c.weekday() in duty_weekdays}

    for slot_time in sorted(candidates):
        if slot_time <= from_time:
            continue
        slot_end = slot_time + timedelta(minutes=duration_minutes)
        # Don't allow slots that run past clinic close
        if slot_end > slot_time.replace(hour=clinic_end, minute=0, second=0, microsecond=0):
            continue
        result = find_best_slot(db, procedure, slot_time, slot_end, buffer_minutes)
        if result:
            return result[0], result[1], slot_time

    return None


# ---------------------------------------------------------------------------
# Booking
# ---------------------------------------------------------------------------

def book_appointment(
    db: Session,
    patient_id: int,
    doctor_id: int,
    room_id: int,
    procedure: str,
    start: datetime,
    end: datetime,
) -> Appointment:
    """
    Create and persist an appointment after confirming no double-booking.
    Raises ValueError if the doctor or room is not available in the window.
    """
    if not is_doctor_available(db, doctor_id, start, end):
        raise ValueError(f"Doctor {doctor_id} is not available from {start} to {end}.")
    if not is_room_available(db, room_id, start, end):
        raise ValueError(f"Room {room_id} is not available from {start} to {end}.")

    appointment = Appointment(
        patient_id=patient_id,
        doctor_id=doctor_id,
        room_id=room_id,
        procedure=procedure,
        start_time=start,
        end_time=end,
        status="scheduled",
    )
    db.add(appointment)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise ValueError(f"Slot at {start} is already booked (unique constraint).")
    db.refresh(appointment)
    return appointment


# ---------------------------------------------------------------------------
# Read helpers — used by the chatbot to report existing bookings
# ---------------------------------------------------------------------------

def get_patient_appointments(db: Session, patient_id: int) -> list[Appointment]:
    """Return all upcoming appointments for a patient, ordered by start time."""
    return (
        db.query(Appointment)
        .filter(
            Appointment.patient_id == patient_id,
            Appointment.start_time >= datetime.now(),
        )
        .order_by(Appointment.start_time)
        .all()
    )


def get_all_appointments(db: Session) -> list[Appointment]:
    """Return all appointments ordered by start time. Useful for admin/debug."""
    return db.query(Appointment).order_by(Appointment.start_time).all()
