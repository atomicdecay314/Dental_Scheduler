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


def find_available_slot(
    db: Session,
    procedure: str,
    start: datetime,
    end: datetime,
    preferred_doctor_id: int | None = None,
) -> tuple[Doctor, Room] | None:
    """
    Find any (doctor, room) pair that can handle the procedure in the given window.
    If a preferred_doctor_id is provided, tries that doctor first.
    Returns None if nothing is available.
    """
    doctors = get_doctors_for_procedure(db, procedure)
    rooms = get_rooms_for_procedure(db, procedure)

    if preferred_doctor_id:
        doctors.sort(key=lambda d: d.id != preferred_doctor_id)
        # moves the preferred doctor to the front without filtering others out

    for doctor in doctors:
        if not is_doctor_available(db, doctor.id, start, end):
            continue
        for room in rooms:
            if is_room_available(db, room.id, start, end):
                return doctor, room

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
