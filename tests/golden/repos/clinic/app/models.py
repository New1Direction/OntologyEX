from sqlalchemy import Column, String, Date, ForeignKey
from sqlalchemy.orm import declarative_base

Base = declarative_base()


class Patient(Base):
    __tablename__ = "patients"
    id = Column(String, primary_key=True)
    first_name = Column(String)
    last_name = Column(String)
    date_of_birth = Column(Date)
    email = Column(String)


class Appointment(Base):
    __tablename__ = "appointments"
    id = Column(String, primary_key=True)
    patient_id = Column(String, ForeignKey("patients.id"))
    starts_at = Column(Date)


class Prescription(Base):
    __tablename__ = "prescriptions"
    id = Column(String, primary_key=True)
    patient_id = Column(String, ForeignKey("patients.id"))
    diagnosis = Column(String)
    medication = Column(String)


class AuditLog(Base):
    __tablename__ = "audit_log"
    id = Column(String, primary_key=True)
    actor = Column(String)
    action = Column(String)
