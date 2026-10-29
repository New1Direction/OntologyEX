CREATE TABLE patients (id TEXT PRIMARY KEY, first_name TEXT, last_name TEXT, date_of_birth DATE, email TEXT);
CREATE TABLE prescriptions (id TEXT PRIMARY KEY, patient_id TEXT, diagnosis TEXT, medication TEXT);
