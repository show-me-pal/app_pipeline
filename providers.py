"""Official job-board API adapters.

All endpoints below are the providers' *documented, sanctioned* JSON APIs:

  - Greenhouse Job Board API   (public, no auth)
      https://boards-api.greenhouse.io/v1/boards/{token}/jobs?content=true
  - Lever Postings API         (public, no auth)
      https://api.lever.co/v0/postings/{company}?mode=json
  - Ashby Job Posting API      (public, no auth)
      https://api.ashbyhq.com/posting-api/job-board/{board}?includeCompensation=true
  - Adzuna Search API          (key-based aggregator across many employers)
      https://api.adzuna.com/v1/api/jobs/{country}/search/1

We query employers/boards you configure (by token/company slug). This keeps us
inside each provider's terms of use and gives clean structured fields instead
of brittle page scraping.
"""
from __future__ import annotations

import time
from typing import Iterable, Optional

import requests

from .base import JobSource, JobPosting, html_to_text, parse_min_years

DEFAULT_TIMEOUT = 20
RETRY_STATUS = {429, 500, 502, 503, 504}


def _get_json(url: str, params: Optional[dict] = None, retries: int = 3) -> dict | list:
    """GET with simple exponential backoff on transient errors."""
    last_exc: Optional[Exception] = None
    for attempt in range(retries):
        try:
            resp = requests.get(url, params=params, timeout=DEFAULT_TIMEOUT,
                                headers={"User-Agent": "job-pipeline/1.0"})
            if resp.status_code in RETRY_STATUS:
                raise requests.HTTPError(f"{resp.status_code} from {url}")
            resp.raise_for_status()
            return resp.json()
        except (requests.RequestException, ValueError) as exc:
            last_exc = exc
            sleep = 2 ** attempt
            time.sleep(sleep)
    raise RuntimeError(f"Failed to fetch {url} after {retries} attempts: {last_exc}")


class GreenhouseSource(JobSource):
    name = "greenhouse"

    def __init__(self, board_tokens: list[str]):
        # board_token is the company slug, e.g. "stripe", "airbnb"
        self.board_tokens = board_tokens

    def fetch(self) -> Iterable[JobPosting]:
        for token in self.board_tokens:
            url = f"https://boards-api.greenhouse.io/v1/boards/{token}/jobs"
            data = _get_json(url, params={"content": "true"})
            for job in data.get("jobs", []):
                desc = html_to_text(job.get("content", ""))
                loc = (job.get("location") or {}).get("name", "") or ""
                yield JobPosting(
                    source=self.name,
                    source_job_id=str(job.get("id")),
                    title=job.get("title", "").strip(),
                    company=token,
                    location=loc,
                    description_text=desc,
                    url=job.get("absolute_url", ""),
                    min_years_experience=parse_min_years(desc),
                    posted_at=job.get("updated_at"),
                    raw=job,
                )


class LeverSource(JobSource):
    name = "lever"

    def __init__(self, companies: list[str]):
        self.companies = companies

    def fetch(self) -> Iterable[JobPosting]:
        for company in self.companies:
            url = f"https://api.lever.co/v0/postings/{company}"
            data = _get_json(url, params={"mode": "json"})
            for job in data:
                # Lever splits description into HTML lists; descriptionPlain is
                # provided on most postings and is exactly what we want.
                desc = job.get("descriptionPlain") or html_to_text(
                    job.get("description", "")
                )
                cats = job.get("categories", {}) or {}
                yield JobPosting(
                    source=self.name,
                    source_job_id=str(job.get("id")),
                    title=job.get("text", "").strip(),
                    company=company,
                    location=cats.get("location", "") or "",
                    description_text=desc,
                    url=job.get("hostedUrl", ""),
                    min_years_experience=parse_min_years(desc),
                    posted_at=(
                        str(job.get("createdAt")) if job.get("createdAt") else None
                    ),
                    raw=job,
                )


class AshbySource(JobSource):
    name = "ashby"

    def __init__(self, board_names: list[str]):
        self.board_names = board_names

    def fetch(self) -> Iterable[JobPosting]:
        for board in self.board_names:
            url = f"https://api.ashbyhq.com/posting-api/job-board/{board}"
            data = _get_json(url, params={"includeCompensation": "true"})
            for job in data.get("jobs", []):
                desc = job.get("descriptionPlain") or html_to_text(
                    job.get("descriptionHtml", "")
                )
                yield JobPosting(
                    source=self.name,
                    source_job_id=str(job.get("id")),
                    title=job.get("title", "").strip(),
                    company=board,
                    location=job.get("location", "") or "",
                    description_text=desc,
                    url=job.get("jobUrl", ""),
                    min_years_experience=parse_min_years(desc),
                    posted_at=job.get("publishedAt"),
                    raw=job,
                )


class AdzunaSource(JobSource):
    """Aggregator API. Requires free app_id / app_key from developer.adzuna.com.

    Good for breadth across many employers via a keyword/location query rather
    than a per-company board token.
    """
    name = "adzuna"

    def __init__(self, app_id: str, app_key: str, country: str = "us",
                 queries: Optional[list[dict]] = None, results_per_query: int = 25):
        self.app_id = app_id
        self.app_key = app_key
        self.country = country
        # each query: {"what": "data engineer", "where": "remote"}
        self.queries = queries or []
        self.results_per_query = results_per_query

    def fetch(self) -> Iterable[JobPosting]:
        for q in self.queries:
            url = f"https://api.adzuna.com/v1/api/jobs/{self.country}/search/1"
            params = {
                "app_id": self.app_id,
                "app_key": self.app_key,
                "results_per_page": self.results_per_query,
                "content-type": "application/json",
                **q,
            }
            data = _get_json(url, params=params)
            for job in data.get("results", []):
                desc = job.get("description", "") or ""
                yield JobPosting(
                    source=self.name,
                    source_job_id=str(job.get("id")),
                    title=job.get("title", "").strip(),
                    company=(job.get("company") or {}).get("display_name", ""),
                    location=(job.get("location") or {}).get("display_name", ""),
                    description_text=desc,
                    url=job.get("redirect_url", ""),
                    min_years_experience=parse_min_years(desc),
                    posted_at=job.get("created"),
                    raw=job,
                )


def dedup(postings: Iterable[JobPosting]) -> list[JobPosting]:
    """Drop duplicate postings by stable key, keeping first seen."""
    seen: set[str] = set()
    out: list[JobPosting] = []
    for p in postings:
        k = p.stable_key()
        if k not in seen:
            seen.add(k)
            out.append(p)
    return out
