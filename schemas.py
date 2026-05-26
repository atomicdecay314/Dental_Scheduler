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
# Chatbot
# ---------------------------------------------------------------------------

class ChatRequest(BaseModel):
    session_id: str
    message: str
