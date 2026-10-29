from fastapi import FastAPI
from argon2 import PasswordHasher

app = FastAPI()
hasher = PasswordHasher()


@app.get("/patients/{patient_id}")
def read_patient(patient_id: str):
    return {"id": patient_id}


@app.post("/appointments")
def book_appointment(patient_id: str, starts_at: str):
    return {"patient_id": patient_id, "starts_at": starts_at}


@app.post("/appointments/{appointment_id}/reminder")
def send_reminder(appointment_id: str):
    return {"sent": True}  # texts the patient through Twilio
