"""End-to-end orchestrator.

Stages:
  1. ingest   -- pull postings from configured official APIs
  2. extract  -- LLM -> structured requirements
  3. filter   -- SQL gate on experience + required skills/certs
  4. score    -- S_total; high scorers -> ready_to_apply review queue
  5/6. tailor -- (run per job you choose to pursue; see tailor/ modules)
  7/8. apply  -- autofill + manual-review queue (run separately, after review)

Run stages independently with flags so you can inspect between them.
"""
from __future__ import annotations

import argparse
import logging
import os

import yaml

from src.ingest.providers import (
    GreenhouseSource, LeverSource, AshbySource, AdzunaSource, dedup,
)
from src.extract.llm_client import LLMClient
from src.extract.skills_extractor import extract_requirements
from src.match.scoring import (
    ScoreWeights, score_job, partition_by_threshold,
)
from src.db import load as db

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("run")


def load_config(path: str) -> dict:
    with open(path) as f:
        return yaml.safe_load(f)


def build_sources(cfg: dict):
    s = cfg.get("sources", {})
    sources = []
    if s.get("greenhouse"):
        sources.append(GreenhouseSource(s["greenhouse"]))
    if s.get("lever"):
        sources.append(LeverSource(s["lever"]))
    if s.get("ashby"):
        sources.append(AshbySource(s["ashby"]))
    if s.get("adzuna", {}).get("queries"):
        a = s["adzuna"]
        sources.append(AdzunaSource(
            app_id=os.environ["ADZUNA_APP_ID"],
            app_key=os.environ["ADZUNA_APP_KEY"],
            country=a.get("country", "us"),
            queries=a["queries"],
        ))
    return sources


def stage_ingest(cfg: dict, db_path: str) -> int:
    all_jobs = []
    for src in build_sources(cfg):
        log.info("Ingesting from %s", src.name)
        all_jobs.extend(src.fetch())
    all_jobs = dedup(all_jobs)
    con = db.connect(db_path)
    n = db.upsert_jobs(con, all_jobs)
    con.close()
    log.info("Ingested %d unique postings", n)
    return n


def stage_extract(db_path: str) -> int:
    llm = LLMClient()
    con = db.connect(db_path)
    rows = con.execute(
        "SELECT j.job_key, j.title, j.company, j.description_text FROM jobs j "
        "LEFT JOIN job_requirements r ON r.job_key = j.job_key "
        "WHERE r.job_key IS NULL").fetchall()
    log.info("Extracting requirements for %d new jobs", len(rows))
    done = 0
    for r in rows:
        try:
            structured = extract_requirements(
                llm, title=r["title"], company=r["company"],
                description=r["description_text"])
            db.save_requirements(con, r["job_key"], structured)
            done += 1
        except Exception as exc:  # noqa: BLE001
            log.warning("extract failed for %s: %s", r["job_key"], exc)
    con.close()
    log.info("Extracted %d", done)
    return done


def stage_score(cfg: dict, db_path: str) -> None:
    sc = cfg["scoring"]
    weights = ScoreWeights(alpha=sc["alpha"], beta=sc["beta"], gamma=sc["gamma"])
    threshold = sc.get("threshold", 0.85)
    skill_weight_map = sc.get("skill_weights", {})
    role_adjacency = {k: set(v) for k, v in sc.get("role_adjacency", {}).items()}
    max_years = cfg["profile"]["max_years_experience"]

    con = db.connect(db_path)
    my_skills = {row["skill"] for row in con.execute("SELECT skill FROM my_skills")}
    prof = con.execute("SELECT * FROM my_profile WHERE id = 1").fetchone()
    my_years = prof["total_years_experience"] if prof else 0.0
    my_role = prof["primary_role_type"] if prof else None

    candidates = db.candidate_jobs(con, max_years)
    log.info("%d jobs passed the hard filter", len(candidates))

    breakdowns = []
    for c in candidates:
        skills = con.execute(
            "SELECT skill, importance FROM job_skills WHERE job_key = ?",
            (c["job_key"],)).fetchall()
        bd = score_job(
            c["job_key"],
            [{"skill": s["skill"], "importance": s["importance"]} for s in skills],
            my_skills,
            my_years=my_years,
            min_years=c["min_years"],
            max_years=None,
            my_role=my_role,
            job_role=c["role_type"],
            weights=weights,
            skill_weight_map=skill_weight_map,
            role_adjacency=role_adjacency,
        )
        db.save_score(con, bd)
        breakdowns.append(bd)

    flagged, _ = partition_by_threshold(breakdowns, threshold)
    for bd in flagged:
        db.queue_for_review(con, bd.job_key, bd.total)
    con.close()
    log.info("%d jobs scored above %.2f -> ready_to_apply (review queue)",
             len(flagged), threshold)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default="config/profile.yaml")
    ap.add_argument("--db", default="jobs.db")
    ap.add_argument("--stage", choices=["init", "ingest", "extract", "score", "all"],
                    default="all")
    args = ap.parse_args()

    if args.stage in ("init", "all"):
        db.init_db(args.db)
        log.info("DB initialized at %s", args.db)
    cfg = load_config(args.config)
    if args.stage in ("ingest", "all"):
        stage_ingest(cfg, args.db)
    if args.stage in ("extract", "all"):
        stage_extract(args.db)
    if args.stage in ("score", "all"):
        stage_score(cfg, args.db)


if __name__ == "__main__":
    main()
