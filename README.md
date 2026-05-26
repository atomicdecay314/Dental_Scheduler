# Apex Dental Scheduler

A stateful AI chatbot for receptionists to book, view, and manage dental appointments — with a live daily timetable and idle-time optimised slot assignment.

---

## Features

- **Conversational booking** — multi-turn chat collects procedure, date/time, and patient name naturally
- **Gemini-powered extraction** — structured intent + entity parsing via `gemini-2.5-flash-lite`
- **Idle-time optimisation** — slot assignment minimises gaps in each doctor's schedule
- **Next-slot suggestion** — when a requested time is unavailable, the nearest free slot is offered automatically
- **Live timetable** — daily grid showing all doctors' schedules, updates on every booking
- **Collapsible chat panel** — side-by-side chat + calendar layout built with Tailwind CSS

---

## Stack

| Layer | Tech |
|---|---|
| API | FastAPI |
| ORM / DB | SQLAlchemy + SQLite |
| AI | Google Gemini (`google-genai`) |
| Frontend | Tailwind CSS + Material Symbols (single-file) |
| Server | Uvicorn |

---

## Setup

**Prerequisites:** Python 3.11+, a virtual environment, a Gemini API key.

```bash
# 1. Install dependencies
.venv/bin/pip install -r requirements.txt

# 2. Create .env with your Gemini key
echo "GEMINI_API_KEY=your_key_here" > .env

# 3. Seed the database (doctors, rooms, sample patients)
.venv/bin/python seed.py

# 4. Start the server
set -a && source .env && set +a && .venv/bin/uvicorn main:app --reload
```

Open `http://127.0.0.1:8000` for the chat UI, or `http://127.0.0.1:8000/docs` for the interactive API docs.

---

## Usage

Type naturally into the chat. Examples:

```
Book a cleaning for Rahul Patil on Friday at 10am
Book an extraction for Priya on 3rd June at 2pm
Show appointments for Dhruv Sawant
```

The assistant collects any missing details (procedure → time → name) one turn at a time, proposes the best available slot, and asks for confirmation before booking.

---

## API Endpoints

| Method | Path | Description |
|---|---|---|
| `POST` | `/chat` | Send a message (`session_id`, `message`) |
| `GET` | `/appointments` | List all appointments (nested patient/doctor/room) |
| `GET` | `/doctors` | List all doctors |
| `GET` | `/slots` | Available slots for a procedure on a date (`?procedure=cleaning&date=2026-05-28`) |
| `DELETE` | `/appointments` | Clear all appointments |

---

## Architecture

```
POST /chat  →  main.py  (Gemini extraction + session state machine)
                   │
                   ├── scheduler.py  (availability, idle-time scoring, booking)
                   │        └── models.py  (Doctor, Room, Patient, Appointment)
                   └── sessions {}   (in-memory state per session_id)
```

**Seeded data:** 3 doctors, 3 rooms, each mapped to specific procedures via association tables (`doctor_procedures`, `room_procedures`). Procedure strings must match exactly across seed data and `VALID_PROCEDURES` in `main.py`.

**Double-booking prevention** is two-layered: range overlap queries in `scheduler.py` catch partial overlaps; DB `UniqueConstraint` on `(doctor_id, start_time)` and `(room_id, start_time)` catches exact-time duplicates.

**Session state** is in-memory (lost on restart). Each session tracks: `procedure`, `start_time`, `patient_name`, `recommended_doctor_id`, `recommended_room_id`, `awaiting_confirmation`.

---

## Procedures

| Procedure | Doctor | Room |
|---|---|---|
| cleaning, filling, xray | Dr. Palash Mehta | Room 1 |
| extraction, implant | Dr. Varun Kumar | Room 2 |
| braces_consultation, retainer_fitting | Dr. Rhea Singh | Room 3 |

To add a new procedure: update `seed.py` (doctor + room associations) and `VALID_PROCEDURES` in `main.py`, then re-seed.
