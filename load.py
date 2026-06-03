"""SQLite persistence helpers tying the pipeline stages to the schema.

SQLite by default for zero-setup portability. For Postgres/your warehouse,
swap the connection and the few SQLite-specific bits (the schema is otherwise
standard SQL).
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

SCHEMA_PATH = Path(__file__).with_name("schema.sql")
FILTER_PATH = Path(__file__).parent.parent / "match" / "filter_jobs.sql"


def connect(db_path: str) -> sqlite3.Connection:
    con = sqlite3.connect(db_path)
    con.execute("PRAGMA foreign_keys = ON")
    con.row_factory = sqlite3.Row
    return con


def init_db(db_path: str) -> None:
    with connect(db_path) as con:
        con.executescript(SCHEMA_PATH.read_text())
        con.commit()


def upsert_jobs(con: sqlite3.Connection, postings: Iterable) -> int:
    rows = 0
    for p in postings:
        d = p.to_dict() if hasattr(p, "to_dict") else p
        con.execute(
            "INSERT OR REPLACE INTO jobs (job_key, source, source_job_id, title, "
            "company, location, description_text, url, min_years_hint, posted_at, "
            "ingested_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (p.stable_key(), d["source"], d["source_job_id"], d["title"],
             d["company"], d["location"], d["description_text"], d["url"],
             d.get("min_years_experience"), d.get("posted_at"), d["ingested_at"]),
        )
        rows += 1
    con.commit()
    return rows


def save_requirements(con: sqlite3.Connection, job_key: str, structured: dict) -> None:
    now = datetime.now(timezone.utc).isoformat()
    exp = structured.get("experience", {})
    con.execute(
        "INSERT OR REPLACE INTO job_requirements (job_key, role_type, min_years, "
        "max_years, seniority, extracted_at) VALUES (?,?,?,?,?,?)",
        (job_key, structured.get("role_type"), exp.get("min_years"),
         exp.get("max_years"), exp.get("seniority"), now),
    )
    con.execute("DELETE FROM job_skills WHERE job_key = ?", (job_key,))
    for s in structured.get("technical_skills", []):
        con.execute(
            "INSERT OR IGNORE INTO job_skills (job_key, skill, importance) "
            "VALUES (?,?,?)", (job_key, s["name"], s["importance"]))
    con.execute("DELETE FROM job_certifications WHERE job_key = ?", (job_key,))
    for c in structured.get("certifications", []):
        con.execute(
            "INSERT OR IGNORE INTO job_certifications (job_key, cert, is_required) "
            "VALUES (?,?,?)", (job_key, c["name"], 1 if c["required"] else 0))
    con.execute("DELETE FROM job_duties WHERE job_key = ?", (job_key,))
    for duty in structured.get("operational_duties", []):
        con.execute("INSERT INTO job_duties (job_key, duty) VALUES (?,?)",
                    (job_key, duty))
    con.commit()


def candidate_jobs(con: sqlite3.Connection, max_years: int) -> list[sqlite3.Row]:
    """Run step-3 filter SQL with the experience ceiling bound."""
    sql = FILTER_PATH.read_text().replace(":max_years_experience", "?")
    return con.execute(sql, (max_years,)).fetchall()


def save_score(con: sqlite3.Connection, breakdown) -> None:
    now = datetime.now(timezone.utc).isoformat()
    r = breakdown.as_row()
    con.execute(
        "INSERT OR REPLACE INTO scored_jobs (job_key, score, skill_term, "
        "exp_term, role_term, scored_at) VALUES (?,?,?,?,?,?)",
        (r["job_key"], r["score"], r["skill_term"], r["exp_term"],
         r["role_term"], now))
    con.commit()


def queue_for_review(con: sqlite3.Connection, job_key: str, score: float) -> None:
    now = datetime.now(timezone.utc).isoformat()
    con.execute(
        "INSERT OR REPLACE INTO ready_to_apply (job_key, score, queued_at) "
        "VALUES (?,?,?)", (job_key, score, now))
    con.commit()
