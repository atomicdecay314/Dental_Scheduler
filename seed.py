from database import Base, engine, SessionLocal
from models import Doctor, Room, Patient, ProcedureConfig, doctor_procedures, room_procedures

Base.metadata.create_all(bind=engine)


def seed():
    db = SessionLocal()
    try:
        # Skip if already seeded — prevents duplicates on repeated runs
        if db.query(Doctor).first():
            print("Database already seeded, skipping.")
            return

        doctors = [
            Doctor(name="Dr. Palash Mehta"),
            Doctor(name="Dr. Varun Kumar"),
            Doctor(name="Dr. Rhea Singh"),
        ]
        db.add_all(doctors)
        db.flush()

        db.execute(doctor_procedures.insert(), [
            {"doctor_id": doctors[0].id, "procedure": "cleaning"},
            {"doctor_id": doctors[0].id, "procedure": "filling"},
            {"doctor_id": doctors[0].id, "procedure": "xray"},
            {"doctor_id": doctors[1].id, "procedure": "extraction"},
            {"doctor_id": doctors[1].id, "procedure": "implant"},
            {"doctor_id": doctors[2].id, "procedure": "braces_consultation"},
            {"doctor_id": doctors[2].id, "procedure": "retainer_fitting"},
        ])

        rooms = [
            Room(name="Room 1"),          # id=1 — general purpose
            Room(name="Room 2"),          # id=2 — surgical
            Room(name="Room 3"),          # id=3 — orthodontics
        ]
        db.add_all(rooms)
        db.flush()

        db.execute(room_procedures.insert(), [
            {"room_id": rooms[0].id, "procedure": "cleaning"},
            {"room_id": rooms[0].id, "procedure": "filling"},
            {"room_id": rooms[0].id, "procedure": "xray"},
            {"room_id": rooms[1].id, "procedure": "extraction"},
            {"room_id": rooms[1].id, "procedure": "implant"},
            {"room_id": rooms[2].id, "procedure": "braces_consultation"},
            {"room_id": rooms[2].id, "procedure": "retainer_fitting"},
        ])

        # Global procedure duration fallbacks (doctor_id=None)
        procedure_configs = [
            ProcedureConfig(doctor_id=None, procedure="cleaning",             duration_minutes=30,  buffer_pct=10.0),
            ProcedureConfig(doctor_id=None, procedure="filling",              duration_minutes=45,  buffer_pct=10.0),
            ProcedureConfig(doctor_id=None, procedure="xray",                 duration_minutes=20,  buffer_pct=10.0),
            ProcedureConfig(doctor_id=None, procedure="extraction",           duration_minutes=60,  buffer_pct=10.0),
            ProcedureConfig(doctor_id=None, procedure="implant",              duration_minutes=90,  buffer_pct=10.0),
            ProcedureConfig(doctor_id=None, procedure="braces_consultation",  duration_minutes=45,  buffer_pct=10.0),
            ProcedureConfig(doctor_id=None, procedure="retainer_fitting",     duration_minutes=30,  buffer_pct=10.0),
            # Per-doctor overrides
            ProcedureConfig(doctor_id=doctors[0].id, procedure="cleaning",   duration_minutes=25,  buffer_pct=10.0),  # Dr. Palash Mehta faster
            ProcedureConfig(doctor_id=doctors[1].id, procedure="extraction",  duration_minutes=75,  buffer_pct=10.0),  # Dr. Varun Kumar slower
        ]
        db.add_all(procedure_configs)

        patients = [
            Patient(name="Dhruv Sawant",    phone="8562202171", email="dhryv@example.com"),
            Patient(name="Ekansh Shukla",  phone="932327575", email="ekansh@example.com"),
            Patient(name="Rahul Patil",  phone="9820228444", email="rahul@example.com"),
        ]
        db.add_all(patients)

        db.commit()
        print("Database seeded successfully.")
    except Exception as e:
        db.rollback()
        raise e
    finally:
        db.close()


if __name__ == "__main__":
    seed()
