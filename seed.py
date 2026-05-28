from database import Base, engine, SessionLocal
from models import Doctor, Room, Patient, ProcedureConfig, DoctorAvailability, doctor_procedures, room_procedures

Base.metadata.create_all(bind=engine)


def seed():
    db = SessionLocal()
    try:
        # Skip if already seeded — prevents duplicates on repeated runs
        if db.query(Doctor).first():
            print("Database already seeded, skipping.")
            return
        if db.query(DoctorAvailability).first():
            print("Availability already seeded, skipping.")
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
            {"doctor_id": doctors[2].id, "procedure": "root_canal"},
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
            {"room_id": rooms[1].id, "procedure": "root_canal"},
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
            ProcedureConfig(doctor_id=None, procedure="root_canal",           duration_minutes=90,  buffer_pct=10.0),
            # Per-doctor overrides
            ProcedureConfig(doctor_id=doctors[0].id, procedure="cleaning",   duration_minutes=25,  buffer_pct=10.0),  # Dr. Palash Mehta faster
            ProcedureConfig(doctor_id=doctors[1].id, procedure="extraction",  duration_minutes=75,  buffer_pct=10.0),  # Dr. Varun Kumar slower
        ]
        db.add_all(procedure_configs)

        # Doctor working hours
        # Dr. Palash Mehta: Mon–Fri 09:00–17:00
        availability = []
        for day in range(5):
            availability.append(DoctorAvailability(doctor_id=doctors[0].id, day_of_week=day, start_time="09:00", end_time="17:00"))
        # Dr. Varun Kumar: Mon/Wed/Fri 09:00–17:00
        for day in [0, 2, 4]:
            availability.append(DoctorAvailability(doctor_id=doctors[1].id, day_of_week=day, start_time="09:00", end_time="17:00"))
        # Dr. Rhea Singh: Tue/Thu/Sat 10:00–16:00
        for day in [1, 3, 5]:
            availability.append(DoctorAvailability(doctor_id=doctors[2].id, day_of_week=day, start_time="10:00", end_time="16:00"))
        db.add_all(availability)

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
