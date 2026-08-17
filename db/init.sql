-- DB schema and sample slot
CREATE EXTENSION IF NOT EXISTS "uuid-ossp";

CREATE TABLE IF NOT EXISTS slots (
  id uuid PRIMARY KEY DEFAULT uuid_generate_v4(),
  start_time timestamptz NOT NULL,
  status text NOT NULL DEFAULT 'open',
  booked_by text,
  booking_id uuid
);

CREATE TABLE IF NOT EXISTS bookings (
  id uuid PRIMARY KEY DEFAULT uuid_generate_v4(),
  slot_id uuid REFERENCES slots(id) ON DELETE CASCADE,
  patient_name text NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now()
);

-- Insert a sample slot far enough in the future
INSERT INTO slots (start_time) VALUES (now() + interval '2 hours');
