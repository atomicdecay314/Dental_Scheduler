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
    patient: PatientRead
    doctor: DoctorRead
    room: RoomRead

    model_config = {"from_attributes": True}


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
# Chatbot
# ---------------------------------------------------------------------------

class ChatRequest(BaseModel):
    session_id: str
    message: str
