from sqlalchemy import Column, Integer, String, DateTime, ForeignKey, Table, UniqueConstraint
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

    # DB-level guard against exact-time double-booking
    __table_args__ = (
        UniqueConstraint("doctor_id", "start_time", name="uq_doctor_slot"),
        UniqueConstraint("room_id", "start_time", name="uq_room_slot"),
    )
