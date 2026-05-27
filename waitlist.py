from datetime import datetime

from sqlalchemy.orm import Session

from models import Appointment, Waitlist
from scheduler import get_duration, get_buffer_minutes, find_best_slot, book_appointment


def add_to_waitlist(
    db: Session,
    patient_id: int,
    procedure: str,
    requested_start: datetime,
    requested_end: datetime,
    preferred_doctor_id: int | None = None,
) -> Waitlist:
    """Add a patient to the waitlist; priority = position in queue for that slot."""
    existing_count = db.query(Waitlist).filter(
        Waitlist.procedure == procedure,
        Waitlist.requested_start == requested_start,
        Waitlist.requested_end == requested_end,
        Waitlist.notified == False,
    ).count()
    entry = Waitlist(
        patient_id=patient_id,
        procedure=procedure,
        doctor_id=preferred_doctor_id,
        requested_start=requested_start,
        requested_end=requested_end,
        priority=existing_count + 1,
        notified=False,
    )
    db.add(entry)
    db.commit()
    db.refresh(entry)
    return entry


def trigger_waitlist(
    db: Session,
    procedure: str,
    freed_start: datetime,
    freed_end: datetime,
) -> list[Appointment]:
    """
    Try to book waitlisted patients for a freed slot, in priority order.
    Marks each successfully booked entry as notified.
    """
    entries = db.query(Waitlist).filter(
        Waitlist.procedure == procedure,
        Waitlist.requested_start >= freed_start,
        Waitlist.requested_end <= freed_end,
        Waitlist.notified == False,
    ).order_by(Waitlist.priority.asc()).all()

    booked: list[Appointment] = []
    for entry in entries:
        try:
            dur, buf_pct = get_duration(db, procedure, entry.doctor_id)
            buf = get_buffer_minutes(dur, buf_pct)
            slot = find_best_slot(db, procedure, entry.requested_start, entry.requested_end, buffer_minutes=buf)
            if slot is None:
                continue
            doctor, room = slot
            appt = book_appointment(
                db,
                patient_id=entry.patient_id,
                doctor_id=doctor.id,
                room_id=room.id,
                procedure=procedure,
                start=entry.requested_start,
                end=entry.requested_end,
            )
            entry.notified = True
            db.commit()
            booked.append(appt)
        except (ValueError, TypeError):
            continue

    return booked
