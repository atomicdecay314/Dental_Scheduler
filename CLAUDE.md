# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

```bash
# Install dependencies
.venv/bin/pip install -r requirements.txt

# Download spaCy model (required once after install)
.venv/bin/python -m spacy download en_core_web_sm

# Seed the database with doctors, rooms, and patients
.venv/bin/python seed.py

# Start the API server (auto-reloads on file changes)
.venv/bin/uvicorn main:app --reload
```

The interactive API docs are available at `http://127.0.0.1:8000/docs` once the server is running.

## Architecture

This is a stateful rule-based chatbot API for booking dental appointments. The stack is FastAPI + SQLAlchemy + SQLite + spaCy + dateparser.

**Request flow:**

```
POST /chat  →  main.py (extract intent/procedure/datetime/name)
                    │
                    ├── scheduler.py  (availability checks + booking)
                    │        └── models.py  (Doctor, Room, Patient, Appointment)
                    └── sessions dict  (in-memory state per session_id)
```

**Key design decisions:**

- `sessions` (in `main.py`) is an in-memory dict keyed by `session_id`. It holds partial booking state (`procedure`, `start_time`, `patient_name`) across turns. This is lost on server restart — production would use Redis.
- Procedure skills for doctors and room capabilities are stored in `doctor_procedures` and `room_procedures` association tables (not columns). The procedure string is the join key between them — it must match exactly across seed data, `PROCEDURES` dict in `main.py`, and any booking request.
- Double-booking prevention is two-layered: DB `UniqueConstraint` on exact `start_time`, plus range overlap queries in `scheduler.py` (`start_time < end AND end_time > start`) that catch partial overlaps.
- `AppointmentRead` in `schemas.py` returns fully nested patient/doctor/room objects — no second request needed.

**Adding a new procedure:** add it to `seed.py` (doctor and room associations), add its synonyms to the `PROCEDURES` dict in `main.py`, then re-seed.

**Chatbot booking flow** (multi-turn, collects one missing field per turn):
1. Extract procedure → ask if missing
2. Extract datetime via `dateparser` → ask if missing
3. Extract patient name via spaCy NER / regex → ask if missing
4. `find_available_slot` → `book_appointment` → confirm or ask for another time
