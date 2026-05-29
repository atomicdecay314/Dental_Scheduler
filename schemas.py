from datetime import datetime
from pydantic import BaseModel


# ---------------------------------------------------------------------------
# Doctor
# ---------------------------------------------------------------------------

class DoctorBase(BaseModel):
    name: str

class DoctorCreate(DoctorBase):
    procedures: list[str]

class DoctorRead(DoctorBase):
    id: int

    model_config = {"from_attributes": True}


# ---------------------------------------------------------------------------
# Room
# ---------------------------------------------------------------------------

class RoomBase(BaseModel):
    name: str

class RoomCreate(RoomBase):
    procedures: list[str]

class RoomRead(RoomBase):
    id: int

    model_config = {"from_attributes": True}


# ---------------------------------------------------------------------------
# Patient
# ---------------------------------------------------------------------------

class PatientBase(BaseModel):
    name: str
    phone: str | None = None
    email: str | None = None

class PatientCreate(PatientBase):
    pass

class PatientRead(PatientBase):
    id: int

    model_config = {"from_attributes": True}


# ---------------------------------------------------------------------------
# Appointment
# ---------------------------------------------------------------------------

class AppointmentCreate(BaseModel):
    patient_id: int
    doctor_id: int
    room_id: int
    procedure: str
    start_time: datetime
    end_time: datetime

class AppointmentRead(BaseModel):
    id: int
    procedure: str
    status: str
    start_time: datetime
    end_time: datetime
    actual_end_time: datetime | None = None
    completed_at: datetime | None = None
    buffer_minutes: int = 0
    patient: PatientRead
    doctor: DoctorRead
    room: RoomRead

    model_config = {"from_attributes": True}


class EarlyEndRequest(BaseModel):
    actual_end_time: datetime


# ---------------------------------------------------------------------------
# Slots
# ---------------------------------------------------------------------------

class SlotOption(BaseModel):
    start_time: datetime
    end_time: datetime
    doctor: str
    doctor_id: int
    room: str
    room_id: int
    idle_time_score: float


# ---------------------------------------------------------------------------
# ProcedureConfig
# ---------------------------------------------------------------------------

class ProcedureConfigBase(BaseModel):
    procedure: str
    duration_minutes: int
    buffer_pct: float = 10.0
    doctor_id: int | None = None

class ProcedureConfigCreate(ProcedureConfigBase):
    pass

class ProcedureConfigRead(ProcedureConfigBase):
    id: int

    model_config = {"from_attributes": True}


# ---------------------------------------------------------------------------
# Waitlist
# ---------------------------------------------------------------------------

class WaitlistRead(BaseModel):
    id: int
    patient_id: int
    patient_name: str
    procedure: str
    doctor_id: int | None
    requested_start: datetime
    requested_end: datetime
    priority: int
    notified: bool
    created_at: datetime

    model_config = {"from_attributes": True}


class TriggerWaitlistRequest(BaseModel):
    procedure: str
    freed_start: datetime
    freed_end: datetime


# ---------------------------------------------------------------------------
# DoctorAvailability
# ---------------------------------------------------------------------------

class DoctorAvailabilityCreate(BaseModel):
    doctor_id: int
    day_of_week: int
    start_time: str
    end_time: str

class DoctorAvailabilityRead(DoctorAvailabilityCreate):
    id: int
    doctor_name: str

    model_config = {"from_attributes": True}


# ---------------------------------------------------------------------------
# DoctorLeave
# ---------------------------------------------------------------------------

class DoctorLeaveCreate(BaseModel):
    doctor_id: int
    date_from: datetime
    date_to: datetime
    reason: str | None = None

class DoctorLeaveRead(DoctorLeaveCreate):
    id: int
    doctor_name: str

    model_config = {"from_attributes": True}


# ---------------------------------------------------------------------------
# Chatbot
# ---------------------------------------------------------------------------

class ChatRequest(BaseModel):
    session_id: str
    message: str
