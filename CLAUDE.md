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
set -a && source .env && set +a && .venv/bin/uvicorn main:app --reload
```

The interactive API docs are available at `http://127.0.0.1:8000/docs` once the server is running. The chat UI is served at `http://127.0.0.1:8000`.

## Environment

Requires a `GEMINI_API_KEY` in `.env`. The server will fail to start without it.

## Architecture

FastAPI + SQLAlchemy (SQLite) + Gemini API chatbot for booking dental appointments.

**Request flow:**

```
POST /chat  →  main.py (session state + Gemini extraction)
                    │
                    ├── scheduler.py  (availability checks + booking logic)
                    │        └── models.py  (Doctor, Room, Patient, Appointment)
                    └── sessions dict  (in-memory per session_id — lost on restart)
```

**Key design decisions:**

- `sessions` in `main.py` is an in-memory dict keyed by `session_id`. It holds partial booking state (`procedure`, `start_time`, `patient_name`, `recommended_doctor_id`, `recommended_room_id`, `awaiting_confirmation`) across turns. Lost on server restart — production would use Redis.
- Gemini (`gemini-2.5-flash-lite`) extracts structured JSON (intent, procedure, datetime, patient_name) from every message. The extraction prompt is in `EXTRACTION_PROMPT` and returns one of: `book | list | cancel | greet | unknown`.
- Procedure-to-doctor and procedure-to-room mappings use `doctor_procedures` and `room_procedures` association tables (not columns). The procedure string is the join key — it must match exactly across seed data and `VALID_PROCEDURES` in `main.py`.
- Double-booking is two-layered: range overlap queries in `scheduler.py` (`start_time < end AND end_time > start`) plus DB `UniqueConstraint` on exact `start_time` as a fallback.
- `find_best_slot` scores (doctor, room) pairs by idle-time minimisation — prefers slots that fill gaps around existing appointments. `find_next_available_slot` scans forward hourly up to 7 days when the requested slot is unavailable.
- `AppointmentRead` in `schemas.py` returns fully nested patient/doctor/room objects — no second request needed.
- The frontend (`static/index.html`) is a single-file Tailwind + Material Symbols UI with a collapsible chat panel and a live daily timetable. It calls `POST /chat`, `GET /appointments`, `GET /doctors`, and `DELETE /appointments`.

**Adding a new procedure:** add it to `seed.py` (doctor and room associations) and to `VALID_PROCEDURES` in `main.py`, then re-seed.

**Chatbot booking flow** (multi-turn, collects one missing field per turn):
1. Extract procedure → ask if missing
2. Extract datetime → ask if missing
3. Extract patient name → ask if missing
4. `find_best_slot` → if taken, `find_next_available_slot` → confirm → `book_appointment`
