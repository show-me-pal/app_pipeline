-- filter_jobs.sql
-- Step 3: join scraped jobs against your internal profile and keep only the
-- viable ones.
--
-- Parameter (bind at execution time):
--   :max_years_experience  -> drop jobs demanding more than this many years
--
-- "Hard skills you do not possess": we treat a *required* skill that is absent
-- from my_skills as a blocker. Preferred / nice_to_have gaps do NOT disqualify
-- (those are handled as soft penalties by the scoring step instead).
--
-- The result set is the candidate pool that flows into scoring (step 4).

WITH
-- Count required skills per job that I am missing.
missing_required AS (
    SELECT
        js.job_key,
        COUNT(*) AS missing_required_count
    FROM job_skills js
    LEFT JOIN my_skills ms
        ON ms.skill = js.skill
    WHERE js.importance = 'required'
      AND ms.skill IS NULL
    GROUP BY js.job_key
),
-- Required certs I lack (expired counts as lacking).
missing_required_certs AS (
    SELECT
        jc.job_key,
        COUNT(*) AS missing_cert_count
    FROM job_certifications jc
    LEFT JOIN my_certifications mc
        ON mc.cert = jc.cert
       AND (mc.expires_on IS NULL OR mc.expires_on >= DATE('now'))
    WHERE jc.is_required = 1
      AND mc.cert IS NULL
    GROUP BY jc.job_key
)
SELECT
    j.job_key,
    j.title,
    j.company,
    j.location,
    j.url,
    r.role_type,
    r.min_years,
    r.seniority,
    COALESCE(mr.missing_required_count, 0)  AS missing_required_skills,
    COALESCE(mrc.missing_cert_count, 0)     AS missing_required_certs
FROM jobs j
JOIN job_requirements r
    ON r.job_key = j.job_key
LEFT JOIN missing_required mr
    ON mr.job_key = j.job_key
LEFT JOIN missing_required_certs mrc
    ON mrc.job_key = j.job_key
WHERE
    -- Experience gate: if the posting states a minimum, it must not exceed
    -- the ceiling you pass in. Jobs with no stated minimum pass through.
    (r.min_years IS NULL OR r.min_years <= :max_years_experience)
    -- Hard-skill gate: must possess every REQUIRED skill.
    AND COALESCE(mr.missing_required_count, 0) = 0
    -- Required-cert gate.
    AND COALESCE(mrc.missing_cert_count, 0) = 0
ORDER BY
    -- Surface closest role-type matches first as a convenience.
    CASE WHEN r.role_type = (SELECT primary_role_type FROM my_profile WHERE id = 1)
         THEN 0 ELSE 1 END,
    j.company,
    j.title;
