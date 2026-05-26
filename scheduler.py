from datetime import datetime
from sqlalchemy.orm import Session
from sqlalchemy import select

from models import Doctor, Room, Patient, Appointment, doctor_procedures, room_procedures


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

def is_doctor_available(db: Session, doctor_id: int, start: datetime, end: datetime) -> bool:
    """True if the doctor has no appointment overlapping the requested window."""
    conflict = db.query(Appointment).filter(
        Appointment.doctor_id == doctor_id,
        Appointment.start_time < end,    # existing appt starts before new one ends
        Appointment.end_time > start,    # existing appt ends after new one starts
    ).first()
    return conflict is None


def is_room_available(db: Session, room_id: int, start: datetime, end: datetime) -> bool:
    """True if the room has no appointment overlapping the requested window."""
    conflict = db.query(Appointment).filter(
        Appointment.room_id == room_id,
        Appointment.start_time < end,
        Appointment.end_time > start,
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
        if not is_doctor_available(db, doctor.id, start, end):
            continue
        for room in rooms:
            if not is_room_available(db, room.id, start, end):
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
    duration_minutes: int = 60,
    clinic_start: int = 9,
    clinic_end: int = 17,
    max_days: int = 7,
) -> tuple[Doctor, Room, datetime] | None:
    """
    Scan forward from from_time (in hourly steps, within clinic hours) to find
    the next available (doctor, room, start) for the procedure.
    Looks up to max_days ahead. Returns None if nothing found.
    """
    slot = from_time.replace(minute=0, second=0, microsecond=0)
    # Start from the next hour if the requested time is already taken
    slot += timedelta(hours=1)

    deadline = from_time + timedelta(days=max_days)

    while slot < deadline:
        # Skip outside clinic hours
        if slot.hour < clinic_start or slot.hour >= clinic_end:
            if slot.hour >= clinic_end:
                slot = slot.replace(hour=clinic_start) + timedelta(days=1)
            else:
                slot = slot.replace(hour=clinic_start)
            continue

        end = slot + timedelta(minutes=duration_minutes)
        result = find_best_slot(db, procedure, slot, end)
        if result:
            return result[0], result[1], slot

        slot += timedelta(hours=1)

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
    )
    db.add(appointment)
    db.commit()
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
