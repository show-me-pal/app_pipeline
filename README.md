# Job Application Pipeline

A data-engineering pipeline that ingests job postings from official job-board
APIs, structures them with an LLM, matches them against your profile, scores the
fit, tailors your resume/cover letter, and pre-fills application forms for your
review.

## Design stance (read this first)

Two deliberate guardrails are built in, because they're what separates a tool
that lands interviews from one that gets your email domain blacklisted:

1. **High scores queue for review, they don't auto-submit.** Step 4 implements
   your exact `S_total` formula and flags jobs above the threshold — into a
   `ready_to_apply` review queue, not an autonomous submit. A single bad parse
   (the extractor misreading "no experience required") should never fire an
   application under your name unseen.
2. **The form bot fills, it doesn't submit.** Step 7 maps your data onto Workday/
   Greenhouse forms and uploads your resume, then pauses for you to eyeball and
   click Submit. Both ATSes prohibit automated submission and actively flag bot
   submissions; CAPTCHAs exist to stop exactly this. Auto-submit is a config flag
   (`AUTO_SUBMIT`) you can flip if you own that risk — but anything ambiguous
   (CAPTCHA, screening question, multi-select) always diverts to manual review.

Ingestion uses **official APIs only** (Greenhouse, Lever, Ashby, Adzuna) — no
HTML scraping — per your choice, which keeps you inside each provider's ToS.

## Architecture

```
ingest (official APIs) ──> jobs table
        │
extract (Claude) ────────> job_requirements / job_skills / job_certifications
        │
filter (SQL, step 3) ────> candidate pool (passes experience + required-skill gates)
        │
score (S_total, step 4) ─> scored_jobs ──(> threshold)──> ready_to_apply  [you review]
        │
tailor (steps 5,6) ──────> tailored resume.md + cover letter  [per job you pursue]
        │
apply (steps 7,8) ───────> autofilled form  [you submit]  +  manual_review queue
```

| Stage | File | What it does |
|---|---|---|
| 1 Ingest | `src/ingest/providers.py` | Pull postings from Greenhouse/Lever/Ashby/Adzuna APIs |
| 2 Extract | `src/extract/skills_extractor.py` | Claude → structured JSON (skills, certs, duties, experience, role) |
| 3 Filter | `src/match/filter_jobs.sql` | Join vs your fact tables; drop over-senior / missing-required-skill jobs |
| 4 Score | `src/match/scoring.py` | `S_total = α·Σ(wᵢ·Mᵢ) + β·E + γ·R`; flag > threshold |
| 5 Resume | `src/tailor/resume_tailor.py` | Reorder/re-emphasize bullets; anti-fabrication checks |
| 6 Cover letter | `src/tailor/cover_letter.py` | Generate from resume + JD |
| 7 Autofill | `src/apply/autofill.py` | Playwright fills Workday/Greenhouse forms, uploads PDF |
| 8 Errors | `src/apply/error_handling.py` | CAPTCHA/unknown field → `manual_review`, loop continues |

## Setup

```bash
pip install -r requirements.txt
playwright install chromium          # only needed for step 7

cp .env.example .env                 # add your ANTHROPIC_API_KEY
cp config/profile.example.yaml config/profile.yaml   # edit sources + weights
```

Set `ANTHROPIC_MODEL` in `.env`. Model strings change over time — check the
current list at https://docs.claude.com/en/docs/about-claude/models. A
Sonnet-class model is the right default: extraction is high-volume and doesn't
need the priciest model.

### Populate your profile (the fact tables)

Step 3 joins postings against tables you own. After `python run.py --stage init`:

```sql
INSERT INTO my_profile VALUES (1, 5.0, 'data engineer');   -- years, primary role
INSERT INTO my_skills (skill, proficiency, years_using) VALUES
  ('python',5,4),('sql',5,5),('dbt',4,2);
INSERT INTO my_certifications (cert, issued_on, expires_on) VALUES
  ('aws certified data analytics','2023-01-01',NULL);
-- my_work_history is optional context for tailoring.
```

Skill names must be **canonical lowercase** so they join against the LLM-
normalized `job_skills.skill` (the extractor is instructed to do this, e.g.
`PostgreSQL → postgresql`).

## Running

```bash
python run.py --stage init      # create schema (run once)
python run.py --stage ingest    # pull postings from configured APIs
python run.py --stage extract   # LLM structuring (costs API calls)
python run.py --stage score     # filter + S_total + populate ready_to_apply
# or:
python run.py --stage all
```

Inspect the review queue between stages:

```sql
SELECT j.title, j.company, r.score, j.url
FROM ready_to_apply r JOIN jobs j USING(job_key)
ORDER BY r.score DESC;
```

### Tailoring (steps 5–6, per job you choose)

```python
from src.extract.llm_client import LLMClient
from src.tailor.resume_tailor import tailor_resume, render_markdown
from src.tailor.cover_letter import generate_cover_letter, CoverLetterInputs

llm = LLMClient()
plan = tailor_resume(llm, base_resume, job_description, job_title)  # raises if it fabricates
resume_md = render_markdown(base_resume, plan)
letter = generate_cover_letter(llm, CoverLetterInputs(
    resume=base_resume, tailoring_plan=plan,
    job_title=job_title, company="Acme", job_description=job_description))
```

`tailor_resume` *raises* if the model invents a bullet id, adds a number not in
the original, or lists a skill you don't have — so a passing run is verified
truthful, not just promised to be.

### Applying (steps 7–8, after you've reviewed)

```python
from src.apply.autofill import Applicant, make_applicant_runner
from src.apply.error_handling import run_application_loop

applicant = Applicant(first_name="Jane", last_name="Doe", email="j@x.com",
    phone="555-1234", github_url="https://github.com/jane",
    resume_pdf_path="out/jane_acme.pdf")

apply_fn = make_applicant_runner(applicant, headless=False)  # watch it work
jobs = [{"url": "...", "job_key": "gh:1"}]   # from ready_to_apply
summary = run_application_loop(jobs, apply_fn, "jobs.db")
```

Log in to Workday/Greenhouse once in the launched browser; the persistent
profile reuses the session. The script fills everything then waits — **you**
review and click Submit. Anything it can't handle confidently lands in
`manual_review`:

```sql
SELECT url, reason, detail FROM manual_review ORDER BY logged_at DESC;
```

## The scoring formula (step 4)

```
S_total = α · ( Σ wᵢ·Mᵢ / Σ wᵢ )  +  β · E  +  γ · R
```

- `wᵢ` per-skill weight (config `skill_weights`, default 1.0) × importance
  multiplier (`required` 1.0 / `preferred` 0.6 / `nice_to_have` 0.3)
- `Mᵢ ∈ {0,1}` skill present in your profile
- The skill term is **normalized** by total available weight so it's in [0,1]
  regardless of how many skills a posting lists
- `E` experience alignment in [0,1]; `R` role-type match in [0,1]
- `α+β+γ` must sum to 1.0 (validated) so `S_total ∈ [0,1]` and `0.85` reads as
  "85% match"

Tune `alpha/beta/gamma`, `skill_weights`, and `threshold` in `config/profile.yaml`.

## Portability

SQLite by default (zero setup). The schema is standard SQL; point
`src/db/load.connect` at Postgres / your warehouse and the same `filter_jobs.sql`
works with minor dialect tweaks (e.g. `DATE('now')` → `CURRENT_DATE`).

## Compliance notes

- Ingestion is official-API-only and sends a descriptive User-Agent.
- Respect each provider's rate limits (backoff is built in).
- Form autofill assumes *your own* authenticated session and leaves submission
  to you. Don't enable `AUTO_SUBMIT` against sites whose ToS forbid automation.
```
