from database import Base, engine, SessionLocal
from models import Doctor, Room, Patient, doctor_procedures, room_procedures

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

        # --- Rooms ---
        # Each room is seeded with a name; its equipment capabilities follow the
        # same association-table pattern as doctors.
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

        # --- Patients ---
        # A small set of sample patients for testing the chatbot flow.
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
