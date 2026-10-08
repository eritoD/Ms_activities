ALTER TABLE activities_api.activities
    ADD COLUMN IF NOT EXISTS capacity integer CHECK (capacity BETWEEN 1 AND 100);
ALTER TABLE activities_api.activities
    ADD COLUMN IF NOT EXISTS accepted_count integer NOT NULL DEFAULT 0 CHECK (accepted_count >= 0);
ALTER TABLE activities_api.activity_applications
    ADD COLUMN IF NOT EXISTS decided_at timestamptz;
