ALTER TABLE activities_api.activities
    ADD COLUMN IF NOT EXISTS updated_at timestamptz;
ALTER TABLE activities_api.activities
    ADD COLUMN IF NOT EXISTS cancelled_at timestamptz;
