-- schema.sql
-- Portable schema (SQLite-compatible; notes inline for Postgres).
-- Holds: scraped jobs, LLM-extracted requirements, your internal profile
-- fact tables, and the work queues used by scoring/apply steps.

-- ----------------------------------------------------------------------------
-- Ingested postings (step 1 output)
-- ----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS jobs (
    job_key            TEXT PRIMARY KEY,        -- "<source>:<source_job_id>"
    source             TEXT NOT NULL,
    source_job_id      TEXT NOT NULL,
    title              TEXT NOT NULL,
    company            TEXT NOT NULL,
    location           TEXT,
    description_text   TEXT,
    url                TEXT,
    min_years_hint     INTEGER,                 -- cheap regex pre-parse
    posted_at          TEXT,
    ingested_at        TEXT NOT NULL
);

-- ----------------------------------------------------------------------------
-- Structured requirements (step 2 output). One row per job; lists are
-- normalized into child tables so we can join on individual skills/certs.
-- ----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS job_requirements (
    job_key        TEXT PRIMARY KEY REFERENCES jobs(job_key),
    role_type      TEXT,
    min_years      INTEGER,
    max_years      INTEGER,
    seniority      TEXT,
    extracted_at   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS job_skills (
    job_key     TEXT NOT NULL REFERENCES jobs(job_key),
    skill       TEXT NOT NULL,                  -- canonical lowercase
    importance  TEXT NOT NULL,                  -- required|preferred|nice_to_have
    PRIMARY KEY (job_key, skill)
);

CREATE TABLE IF NOT EXISTS job_certifications (
    job_key     TEXT NOT NULL REFERENCES jobs(job_key),
    cert        TEXT NOT NULL,
    is_required INTEGER NOT NULL,               -- 0/1
    PRIMARY KEY (job_key, cert)
);

CREATE TABLE IF NOT EXISTS job_duties (
    job_key  TEXT NOT NULL REFERENCES jobs(job_key),
    duty     TEXT NOT NULL
);

-- ----------------------------------------------------------------------------
-- YOUR internal fact tables. Populate these once from your own records.
-- (In a warehouse these may already exist; adapt names in the queries below.)
-- ----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS my_skills (
    skill            TEXT PRIMARY KEY,          -- canonical lowercase, matches job_skills.skill
    proficiency      INTEGER,                   -- 1..5 self-rating
    years_using      REAL
);

CREATE TABLE IF NOT EXISTS my_certifications (
    cert             TEXT PRIMARY KEY,
    issued_on        TEXT,
    expires_on       TEXT
);

CREATE TABLE IF NOT EXISTS my_work_history (
    id               INTEGER PRIMARY KEY,
    role_type        TEXT,                      -- matches job_requirements.role_type vocabulary
    title            TEXT,
    start_date       TEXT,
    end_date         TEXT                       -- NULL = current
);

-- Single-row profile summary for fast experience math.
CREATE TABLE IF NOT EXISTS my_profile (
    id                       INTEGER PRIMARY KEY CHECK (id = 1),
    total_years_experience   REAL NOT NULL,
    primary_role_type        TEXT
);

-- ----------------------------------------------------------------------------
-- Queues written by later steps.
-- ----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS scored_jobs (
    job_key      TEXT PRIMARY KEY REFERENCES jobs(job_key),
    score        REAL NOT NULL,
    skill_term   REAL,
    exp_term     REAL,
    role_term    REAL,
    scored_at    TEXT NOT NULL
);

-- step 4: high scorers land here for YOU to review (not auto-submitted).
CREATE TABLE IF NOT EXISTS ready_to_apply (
    job_key      TEXT PRIMARY KEY REFERENCES jobs(job_key),
    score        REAL NOT NULL,
    queued_at    TEXT NOT NULL
);

-- step 8: anything the autofill step can't handle confidently.
CREATE TABLE IF NOT EXISTS manual_review (
    id           INTEGER PRIMARY KEY,
    job_key      TEXT REFERENCES jobs(job_key),
    url          TEXT NOT NULL,
    reason       TEXT NOT NULL,                 -- 'captcha'|'unknown_field'|'screening_question'|...
    detail       TEXT,
    logged_at    TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_job_skills_skill ON job_skills(skill);
CREATE INDEX IF NOT EXISTS idx_jobs_company ON jobs(company);
