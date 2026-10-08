CREATE TABLE IF NOT EXISTS activities_api.activity_applications (
    id uuid PRIMARY KEY,
    activity_id uuid NOT NULL REFERENCES activities_api.activities (id) ON DELETE CASCADE,
    applicant_id uuid NOT NULL,
    status varchar(20) NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'accepted', 'rejected')),
    created_at timestamptz NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (activity_id, applicant_id)
);
CREATE INDEX IF NOT EXISTS activity_applications_activity_idx
    ON activities_api.activity_applications (activity_id, created_at, id);
