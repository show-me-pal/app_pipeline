"""Step 5 -- tailor a base resume to a specific job description.

The model REORDERS and RE-EMPHASIZES existing achievements; it must not invent
employers, dates, metrics, titles, or skills. We enforce this two ways:
  1. A hard instruction in the prompt.
  2. A structured input where every bullet carries a stable `id`, and the model
     returns an ordering + emphasis decisions referencing those ids, so it
     physically cannot author new bullet text out of whole cloth. Optional
     light rewording is allowed only as `reworded` and is validated to share
     enough tokens with the original (anti-fabrication check).

Input resume shape (JSON):
{
  "name": "...", "headline": "...", "contact": {...},
  "sections": [
    {"name": "Experience",
     "entries": [
        {"org": "...", "title": "...", "dates": "...",
         "bullets": [{"id": "b1", "text": "..."}, ...]}
     ]}
  ],
  "skills": ["python", "sql", ...]
}
"""
from __future__ import annotations

import json
from typing import Any, TYPE_CHECKING

if TYPE_CHECKING:  # avoid importing the SDK for the pure helper functions
    from ..extract.llm_client import LLMClient

TAILOR_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "headline": {"type": "string", "description": "tailored one-line headline, may reword but keep truthful"},
        "summary": {"type": "string", "description": "2-3 sentence summary emphasizing relevant strengths"},
        "entries": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "entry_ref": {"type": "string", "description": "org|title identifier from input"},
                    "ordered_bullets": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "id": {"type": "string", "description": "MUST be an id from the input"},
                                "reworded": {"type": ["string", "null"],
                                             "description": "optional light rewording of the SAME achievement; null to keep original"},
                                "emphasis": {"type": "string", "enum": ["high", "normal"]},
                            },
                            "required": ["id", "reworded", "emphasis"],
                        },
                    },
                },
                "required": ["entry_ref", "ordered_bullets"],
            },
        },
        "skills_order": {
            "type": "array",
            "items": {"type": "string"},
            "description": "reordered subset of the EXISTING skills, most relevant first",
        },
    },
    "required": ["headline", "summary", "entries", "skills_order"],
    "additionalProperties": False,
}

SYSTEM = (
    "You tailor resumes by reordering and lightly rephrasing the candidate's "
    "OWN achievements to match a target job. Absolute rules:\n"
    "1. Never invent employers, titles, dates, metrics, tools, or outcomes.\n"
    "2. Every bullet you reference must use an id that exists in the input.\n"
    "3. 'reworded' may only restate the SAME fact in stronger language; it may "
    "not add numbers, tools, or claims not present in the original bullet.\n"
    "4. 'skills_order' may only contain skills already in the input skill list.\n"
    "Prioritize bullets and skills that align with the job's stated technical "
    "and operational requirements."
)


def _flatten_bullets(resume: dict) -> dict[str, str]:
    out: dict[str, str] = {}
    for sec in resume.get("sections", []):
        for entry in sec.get("entries", []):
            for b in entry.get("bullets", []):
                out[b["id"]] = b["text"]
    return out


def _tokens(s: str) -> set[str]:
    return {t for t in "".join(c.lower() if c.isalnum() else " " for c in s).split()
            if len(t) > 2}


def validate_no_fabrication(resume: dict, plan: dict, *, min_overlap: float = 0.4
                            ) -> list[str]:
    """Return a list of violation strings (empty == clean).

    Checks: every referenced id exists; rewordings share enough tokens with the
    original to be a rephrase rather than a new claim; skills_order is a subset.
    """
    problems: list[str] = []
    bullets = _flatten_bullets(resume)
    valid_skills = {s.lower() for s in resume.get("skills", [])}

    for entry in plan.get("entries", []):
        for ob in entry.get("ordered_bullets", []):
            bid = ob.get("id")
            if bid not in bullets:
                problems.append(f"references unknown bullet id '{bid}'")
                continue
            reworded = ob.get("reworded")
            if reworded:
                orig_t, new_t = _tokens(bullets[bid]), _tokens(reworded)
                if not orig_t:
                    continue
                overlap = len(orig_t & new_t) / max(1, len(orig_t))
                if overlap < min_overlap:
                    problems.append(
                        f"bullet '{bid}' reworded too far from original "
                        f"(overlap {overlap:.2f} < {min_overlap})"
                    )
                # Flag suspicious new digits not present in the original.
                new_nums = {t for t in new_t if any(c.isdigit() for c in t)}
                orig_nums = {t for t in orig_t if any(c.isdigit() for c in t)}
                if new_nums - orig_nums:
                    problems.append(
                        f"bullet '{bid}' rewording introduced new numbers: "
                        f"{sorted(new_nums - orig_nums)}"
                    )
    for sk in plan.get("skills_order", []):
        if sk.lower() not in valid_skills:
            problems.append(f"skills_order contains non-existent skill '{sk}'")
    return problems


def tailor_resume(llm: "LLMClient", resume: dict, job_description: str,
                  job_title: str = "") -> dict:
    """Return a tailoring plan (validated). Raises if fabrication detected."""
    prompt = (
        f"TARGET JOB TITLE: {job_title}\n\n"
        f"TARGET JOB DESCRIPTION:\n\"\"\"\n{job_description}\n\"\"\"\n\n"
        f"CANDIDATE RESUME (JSON):\n{json.dumps(resume, indent=2)}\n\n"
        "Produce a tailoring plan per the tool schema. Reorder bullets to put "
        "the most relevant achievements first; set emphasis='high' for the "
        "strongest matches. Keep everything truthful."
    )
    plan = llm.structured(prompt, TAILOR_SCHEMA, tool_name="emit_tailoring",
                          system=SYSTEM, max_tokens=2500)
    problems = validate_no_fabrication(resume, plan)
    if problems:
        raise ValueError("Tailoring failed anti-fabrication checks:\n  - "
                         + "\n  - ".join(problems))
    return plan


def render_markdown(resume: dict, plan: dict) -> str:
    """Apply the validated plan to produce a standard Markdown resume."""
    bullets = _flatten_bullets(resume)
    entry_index = {}
    for sec in resume.get("sections", []):
        for e in sec.get("entries", []):
            entry_index[f"{e.get('org','')}|{e.get('title','')}"] = e

    lines: list[str] = []
    name = resume.get("name", "")
    lines.append(f"# {name}")
    if plan.get("headline"):
        lines.append(f"**{plan['headline']}**")
    contact = resume.get("contact", {})
    if contact:
        lines.append(" | ".join(str(v) for v in contact.values() if v))
    lines.append("")
    if plan.get("summary"):
        lines.append("## Summary")
        lines.append(plan["summary"])
        lines.append("")
    if plan.get("skills_order"):
        lines.append("## Skills")
        lines.append(", ".join(plan["skills_order"]))
        lines.append("")

    lines.append("## Experience")
    for entry_plan in plan.get("entries", []):
        ref = entry_plan["entry_ref"]
        src = entry_index.get(ref, {})
        org = src.get("org", ref.split("|")[0])
        title = src.get("title", "")
        dates = src.get("dates", "")
        header = f"### {title}, {org}".strip(", ")
        if dates:
            header += f"  _{dates}_"
        lines.append(header)
        for ob in entry_plan["ordered_bullets"]:
            text = ob.get("reworded") or bullets.get(ob["id"], "")
            prefix = "- **" if ob.get("emphasis") == "high" else "- "
            suffix = "**" if ob.get("emphasis") == "high" else ""
            lines.append(f"{prefix}{text}{suffix}")
        lines.append("")
    return "\n".join(lines).strip() + "\n"
