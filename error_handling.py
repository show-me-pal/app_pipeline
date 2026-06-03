"""Step 8 -- robust error-handling protocol for the application loop.

The contract: when the autofill step hits anything it cannot handle with
confidence -- a CAPTCHA, an unknown multi-select, an unexpected screening
question, a changed page structure, a timeout -- it must NOT guess. It logs the
job + URL + reason to the `manual_review` queue and lets the loop continue to
the next job. One unhandled form never aborts the batch.
"""
from __future__ import annotations

import logging
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from enum import Enum
from typing import Iterator, Optional

logger = logging.getLogger("apply.errors")


class ReviewReason(str, Enum):
    CAPTCHA = "captcha"
    UNKNOWN_FIELD = "unknown_field"
    SCREENING_QUESTION = "screening_question"
    UNMAPPED_MULTISELECT = "unmapped_multiselect"
    UPLOAD_FAILED = "upload_failed"
    PAGE_STRUCTURE_CHANGED = "page_structure_changed"
    TIMEOUT = "timeout"
    AUTH_REQUIRED = "auth_required"
    UNKNOWN_ERROR = "unknown_error"


class ManualReviewRequired(Exception):
    """Raised inside autofill to bail out of a single job cleanly."""

    def __init__(self, reason: ReviewReason, detail: str = ""):
        self.reason = reason
        self.detail = detail
        super().__init__(f"{reason.value}: {detail}")


@contextmanager
def _conn(db_path: str) -> Iterator[sqlite3.Connection]:
    con = sqlite3.connect(db_path)
    try:
        yield con
        con.commit()
    finally:
        con.close()


def log_for_manual_review(db_path: str, *, url: str, reason: ReviewReason,
                          job_key: Optional[str] = None, detail: str = "") -> None:
    """Persist a manual-review item. Safe to call repeatedly (idempotent-ish:
    we de-dupe on (job_key, reason) to avoid pile-ups on retries)."""
    now = datetime.now(timezone.utc).isoformat()
    with _conn(db_path) as con:
        cur = con.execute(
            "SELECT 1 FROM manual_review WHERE job_key IS ? AND reason = ? LIMIT 1",
            (job_key, reason.value),
        )
        if cur.fetchone():
            logger.info("manual_review already has %s for %s", reason.value, job_key)
            return
        con.execute(
            "INSERT INTO manual_review (job_key, url, reason, detail, logged_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (job_key, url, reason.value, detail[:2000], now),
        )
    logger.warning("Queued for manual review [%s]: %s (%s)", reason.value, url, detail)


def run_application_loop(jobs, apply_fn, db_path: str) -> dict:
    """Drive the apply step over many jobs with per-job isolation.

    `apply_fn(job)` performs the autofill for one job and may raise
    ManualReviewRequired (expected) or any other Exception (unexpected). Either
    way we log and move on. Returns a summary dict.

    `jobs` is an iterable of objects/dicts exposing .url and .job_key (or keys).
    """
    summary = {"attempted": 0, "filled_ok": 0, "manual_review": 0, "errors": 0}

    for job in jobs:
        url = _attr(job, "url")
        job_key = _attr(job, "job_key")
        summary["attempted"] += 1
        try:
            apply_fn(job)
            summary["filled_ok"] += 1
            logger.info("Filled (awaiting your review/submit): %s", url)

        except ManualReviewRequired as mr:
            log_for_manual_review(db_path, url=url, reason=mr.reason,
                                  job_key=job_key, detail=mr.detail)
            summary["manual_review"] += 1
            continue  # smoothly transition to the next job

        except Exception as exc:  # noqa: BLE001 - never let one job kill the loop
            logger.exception("Unexpected error on %s", url)
            log_for_manual_review(db_path, url=url,
                                  reason=ReviewReason.UNKNOWN_ERROR,
                                  job_key=job_key, detail=repr(exc))
            summary["errors"] += 1
            continue

    logger.info("Apply loop done: %s", summary)
    return summary


def _attr(obj, name: str):
    if isinstance(obj, dict):
        return obj.get(name, "")
    return getattr(obj, name, "")
