CREATE TABLE IF NOT EXISTS activities_api.ratings (
    id uuid PRIMARY KEY,
    rater_id uuid NOT NULL,
    rated_id uuid NOT NULL,
    score smallint NOT NULL CHECK (score BETWEEN 1 AND 5),
    comment varchar(300) NOT NULL DEFAULT '',
    created_at timestamptz NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at timestamptz NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (rater_id, rated_id),
    CHECK (rater_id <> rated_id)
);
CREATE INDEX IF NOT EXISTS ratings_rated_idx ON activities_api.ratings (rated_id, updated_at DESC);
CREATE INDEX IF NOT EXISTS activity_applications_applicant_idx
    ON activities_api.activity_applications (applicant_id, status);
