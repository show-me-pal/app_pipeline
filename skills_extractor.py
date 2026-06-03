"""Step 2 -- unstructured description text -> structured JSON.

For each posting we ask Claude to extract:
  - technical_skills  (normalized, deduped, with an importance hint)
  - certifications    (required vs preferred)
  - operational_duties
  - experience        (min/max years, seniority)
  - role_type         (coarse bucket used later for role-match scoring)

The schema is enforced via tool-use so output is always machine-parseable.
We normalize skill names to lowercase canonical tokens so they join cleanly
against your internal skills table later (step 3).
"""
from __future__ import annotations

from typing import Any, Optional

from .llm_client import LLMClient

# JSON Schema the model must fill. Keep it strict; unknown extras are dropped.
EXTRACTION_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "technical_skills": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "name": {"type": "string",
                             "description": "canonical skill, lowercase, e.g. 'python', 'apache airflow', 'dbt'"},
                    "importance": {"type": "string", "enum": ["required", "preferred", "nice_to_have"]},
                },
                "required": ["name", "importance"],
            },
        },
        "certifications": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "required": {"type": "boolean"},
                },
                "required": ["name", "required"],
            },
        },
        "operational_duties": {
            "type": "array",
            "items": {"type": "string"},
            "description": "short verb-led phrases, e.g. 'build ETL pipelines', 'own data quality SLAs'",
        },
        "experience": {
            "type": "object",
            "properties": {
                "min_years": {"type": ["integer", "null"]},
                "max_years": {"type": ["integer", "null"]},
                "seniority": {"type": "string",
                              "enum": ["intern", "junior", "mid", "senior", "lead", "principal", "unspecified"]},
            },
            "required": ["min_years", "max_years", "seniority"],
        },
        "role_type": {
            "type": "string",
            "description": "coarse role bucket, lowercase, e.g. 'data engineer', 'analytics engineer', 'data analyst', 'ml engineer', 'software engineer'",
        },
    },
    "required": ["technical_skills", "certifications", "operational_duties",
                 "experience", "role_type"],
    "additionalProperties": False,
}

SYSTEM = (
    "You are an information-extraction engine for job postings. "
    "Extract only what the text supports. Do not infer skills that are not "
    "mentioned. Normalize skill and tool names to their common lowercase form "
    "(e.g. 'PostgreSQL' -> 'postgresql', 'AWS Redshift' -> 'redshift'). "
    "If a field is genuinely absent, use null or an empty list rather than guessing."
)


def _prompt(title: str, company: str, description: str) -> str:
    return (
        f"Job title: {title}\n"
        f"Company: {company}\n\n"
        f"Job description:\n\"\"\"\n{description}\n\"\"\"\n\n"
        "Extract the structured fields defined by the tool schema."
    )


def extract_requirements(llm: LLMClient, *, title: str, company: str,
                         description: str) -> dict[str, Any]:
    """Return the structured requirement dict for one posting."""
    return llm.structured(
        prompt=_prompt(title, company, description),
        schema=EXTRACTION_SCHEMA,
        tool_name="emit_requirements",
        system=SYSTEM,
        max_tokens=1500,
    )


def extract_batch(llm: LLMClient, postings: list[dict],
                  on_error: Optional[Any] = None) -> list[dict]:
    """Extract for a list of posting dicts (each must have title/company/
    description_text/stable key). Returns list of {key, structured} records.

    Failures are isolated: one bad posting doesn't kill the batch.
    """
    results: list[dict] = []
    for p in postings:
        key = p.get("stable_key") or f"{p.get('source')}:{p.get('source_job_id')}"
        try:
            structured = extract_requirements(
                llm,
                title=p["title"],
                company=p["company"],
                description=p["description_text"],
            )
            results.append({"key": key, "structured": structured})
        except Exception as exc:  # noqa: BLE001 - isolate per-record failures
            if on_error:
                on_error(key, exc)
            results.append({"key": key, "structured": None, "error": str(exc)})
    return results
