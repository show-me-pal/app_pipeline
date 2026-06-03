"""Common data model and source interface for job ingestion.

We ingest *only* from official, sanctioned job-board APIs (Greenhouse, Lever,
Ashby, Adzuna). No HTML scraping. Each provider exposes a public or key-based
JSON endpoint, so we get clean structured data and stay inside their ToS.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from typing import Any, Iterable, Optional


# ---------------------------------------------------------------------------
# Canonical record. Every provider adapter normalizes into this shape so the
# rest of the pipeline never needs to know which board a job came from.
# ---------------------------------------------------------------------------
@dataclass
class JobPosting:
    source: str                      # "greenhouse" | "lever" | "ashby" | "adzuna"
    source_job_id: str               # provider's own id (used for dedup)
    title: str
    company: str
    location: str
    description_text: str            # raw, plain-text job description
    url: str
    min_years_experience: Optional[int] = None   # parsed best-effort, may be None
    posted_at: Optional[str] = None               # ISO-8601 string if available
    raw: dict[str, Any] = field(default_factory=dict)  # untouched provider payload
    ingested_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )

    def stable_key(self) -> str:
        """Deterministic dedup key across re-runs."""
        return f"{self.source}:{self.source_job_id}"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# ---------------------------------------------------------------------------
# Best-effort experience parser. The LLM step (extract/) does the authoritative
# extraction; this is only a cheap pre-filter hint so we can drop obviously
# over-senior roles before paying for an API call.
# ---------------------------------------------------------------------------
_YEARS_PATTERNS = [
    re.compile(r"(\d{1,2})\s*\+?\s*(?:years?|yrs?)", re.I),
    re.compile(r"minimum\s+of\s+(\d{1,2})\s*(?:years?|yrs?)", re.I),
    re.compile(r"at\s+least\s+(\d{1,2})\s*(?:years?|yrs?)", re.I),
]


def parse_min_years(text: str) -> Optional[int]:
    """Return the smallest plausible 'years of experience' figure found, or None.

    Deliberately conservative: we take the *minimum* match because a posting
    often says "3-5 years" or lists several thresholds, and we want the floor
    for filtering. Caps at 0..40 to ignore garbage matches.
    """
    if not text:
        return None
    candidates: list[int] = []
    for pat in _YEARS_PATTERNS:
        for m in pat.finditer(text):
            try:
                n = int(m.group(1))
            except (ValueError, IndexError):
                continue
            if 0 <= n <= 40:
                candidates.append(n)
    return min(candidates) if candidates else None


def html_to_text(html: str) -> str:
    """Minimal HTML-to-text without extra deps.

    Provider APIs return description bodies as HTML fragments. We strip tags and
    collapse whitespace. (BeautifulSoup would be nicer but we keep ingest
    dependency-light; the LLM step tolerates rough text fine.)
    """
    if not html:
        return ""
    text = re.sub(r"(?i)<br\s*/?>", "\n", html)
    text = re.sub(r"(?i)</(p|div|li|h[1-6])>", "\n", text)
    text = re.sub(r"<[^>]+>", "", text)
    # Unescape the few entities that actually show up in job posts.
    for ent, ch in (("&amp;", "&"), ("&nbsp;", " "), ("&#39;", "'"),
                    ("&quot;", '"'), ("&lt;", "<"), ("&gt;", ">")):
        text = text.replace(ent, ch)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


class JobSource:
    """Abstract provider adapter. Subclasses implement `fetch`."""

    name: str = "base"

    def fetch(self) -> Iterable[JobPosting]:  # pragma: no cover - interface
        raise NotImplementedError
