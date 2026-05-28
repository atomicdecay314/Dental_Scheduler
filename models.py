from datetime import datetime as _dt
from sqlalchemy import Column, Integer, String, DateTime, Float, Boolean, ForeignKey, Table, UniqueConstraint, event
from sqlalchemy.schema import DDL
from sqlalchemy.orm import relationship
from database import Base

# Many-to-many: which procedures each doctor is qualified to perform
doctor_procedures = Table(
    "doctor_procedures",
    Base.metadata,
    Column("doctor_id", Integer, ForeignKey("doctors.id"), primary_key=True),
    Column("procedure", String, primary_key=True),
)

# Many-to-many: which procedures each room is equipped for
room_procedures = Table(
    "room_procedures",
    Base.metadata,
    Column("room_id", Integer, ForeignKey("rooms.id"), primary_key=True),
    Column("procedure", String, primary_key=True),
)


class Doctor(Base):
    __tablename__ = "doctors"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String, nullable=False)

    appointments = relationship("Appointment", back_populates="doctor")


class Room(Base):
    __tablename__ = "rooms"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String, nullable=False)

    appointments = relationship("Appointment", back_populates="room")


class Patient(Base):
    __tablename__ = "patients"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String, nullable=False)
    phone = Column(String, nullable=True)
    email = Column(String, nullable=True)

    appointments = relationship("Appointment", back_populates="patient")


class Appointment(Base):
    __tablename__ = "appointments"

    id = Column(Integer, primary_key=True, index=True)
    patient_id = Column(Integer, ForeignKey("patients.id"), nullable=False)
    doctor_id = Column(Integer, ForeignKey("doctors.id"), nullable=False)
    room_id = Column(Integer, ForeignKey("rooms.id"), nullable=False)
    procedure = Column(String, nullable=False)
    start_time = Column(DateTime, nullable=False)
    end_time = Column(DateTime, nullable=False)

    patient = relationship("Patient", back_populates="appointments")
    doctor = relationship("Doctor", back_populates="appointments")
    room = relationship("Room", back_populates="appointments")

    status = Column(String, default="scheduled", nullable=False)

    # No table-level unique constraints — see partial indexes below (status-aware)


class DoctorAvailability(Base):
    __tablename__ = "doctor_availability"

    id         = Column(Integer, primary_key=True, index=True)
    doctor_id  = Column(Integer, ForeignKey("doctors.id"), nullable=False)
    day_of_week = Column(Integer, nullable=False)   # 0=Monday … 6=Sunday
    start_time = Column(String, nullable=False)     # "09:00"
    end_time   = Column(String, nullable=False)     # "17:00"

    doctor = relationship("Doctor")

    __table_args__ = (UniqueConstraint("doctor_id", "day_of_week", name="uq_doctor_day"),)


class DoctorLeave(Base):
    __tablename__ = "doctor_leave"

    id        = Column(Integer, primary_key=True, index=True)
    doctor_id = Column(Integer, ForeignKey("doctors.id"), nullable=False)
    date_from = Column(DateTime, nullable=False)
    date_to   = Column(DateTime, nullable=False)
    reason    = Column(String, nullable=True)

    doctor = relationship("Doctor")


class ProcedureConfig(Base):
    __tablename__ = "procedure_configs"

    id = Column(Integer, primary_key=True, index=True)
    doctor_id = Column(Integer, ForeignKey("doctors.id"), nullable=True)
    procedure = Column(String, nullable=False)
    duration_minutes = Column(Integer, nullable=False)
    buffer_pct = Column(Float, default=10.0, nullable=False)

    doctor = relationship("Doctor")


class Waitlist(Base):
    __tablename__ = "waitlist"

    id = Column(Integer, primary_key=True, index=True)
    patient_id = Column(Integer, ForeignKey("patients.id"), nullable=False)
    procedure = Column(String, nullable=False)
    doctor_id = Column(Integer, ForeignKey("doctors.id"), nullable=True)
    requested_start = Column(DateTime, nullable=False)
    requested_end = Column(DateTime, nullable=False)
    priority = Column(Integer, nullable=False)
    notified = Column(Boolean, default=False, nullable=False)
    created_at = Column(DateTime, default=_dt.utcnow, nullable=False)

    patient = relationship("Patient")
    doctor = relationship("Doctor")


# Partial unique indexes — only enforce uniqueness for non-cancelled appointments.
# This lets a cancelled slot at start_time X be re-booked without constraint errors.
event.listen(
    Base.metadata,
    "after_create",
    DDL(
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_doctor_slot_active "
        "ON appointments (doctor_id, start_time) WHERE status != 'cancelled'"
    ),
)
event.listen(
    Base.metadata,
    "after_create",
    DDL(
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_room_slot_active "
        "ON appointments (room_id, start_time) WHERE status != 'cancelled'"
    ),
)
