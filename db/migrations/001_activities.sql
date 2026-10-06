CREATE SCHEMA IF NOT EXISTS activities_api;
CREATE TABLE IF NOT EXISTS activities_api.activities (
    id uuid PRIMARY KEY,
    organizer_id uuid NOT NULL,
    client_activity_id uuid NOT NULL,
    title varchar(120) NOT NULL CHECK (length(btrim(title)) >= 3),
    sport_code varchar(50) NOT NULL CHECK (sport_code ~ '^[a-z0-9]+(_[a-z0-9]+)*$'),
    description varchar(2000) NOT NULL DEFAULT '',
    starts_at timestamptz NOT NULL,
    location varchar(200) NOT NULL CHECK (length(btrim(location)) >= 3),
    created_at timestamptz NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (organizer_id, client_activity_id)
);
CREATE INDEX IF NOT EXISTS activities_start_idx ON activities_api.activities (starts_at, id);
