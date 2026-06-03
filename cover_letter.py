"""Step 6 -- generate a tailored cover letter.

Modular function: takes the tailored-resume JSON (output of step 5) plus the
job description and company facts, and produces a cover letter that connects
the candidate's analytical background to the company's stated needs.

Same anti-fabrication stance as step 5: the letter may only draw on facts
present in the resume/plan. We pass the resume as the sole source of biographical
truth and instruct the model not to invent.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from ..extract.llm_client import LLMClient


@dataclass
class CoverLetterInputs:
    resume: dict                      # full base resume JSON (source of truth)
    tailoring_plan: dict              # output of step 5 (what to emphasize)
    job_title: str
    company: str
    job_description: str
    hiring_manager: Optional[str] = None
    tone: str = "professional"        # "professional" | "warm" | "concise"
    max_words: int = 320


SYSTEM = (
    "You write concise, specific cover letters. Rules:\n"
    "- Use ONLY facts found in the provided resume JSON. Do not invent "
    "achievements, employers, metrics, or skills.\n"
    "- Open with a concrete hook tied to the company's stated needs, not a "
    "generic 'I am writing to apply'.\n"
    "- Draw an explicit line from the candidate's analytical/technical "
    "background to one or two specific problems the job description names.\n"
    "- No clichés ('team player', 'fast learner', 'passionate'). Show, don't tell.\n"
    "- Plain text, 3-4 short paragraphs."
)


def generate_cover_letter(llm: "LLMClient", inputs: CoverLetterInputs) -> str:
    """Return the cover letter body as plain text."""
    greeting_hint = (
        f"Address it to {inputs.hiring_manager}."
        if inputs.hiring_manager
        else "Use a neutral greeting (e.g. 'Dear Hiring Team') since no name is given."
    )
    prompt = (
        f"COMPANY: {inputs.company}\n"
        f"ROLE: {inputs.job_title}\n"
        f"TONE: {inputs.tone}\n"
        f"WORD LIMIT: {inputs.max_words}\n"
        f"{greeting_hint}\n\n"
        f"JOB DESCRIPTION:\n\"\"\"\n{inputs.job_description}\n\"\"\"\n\n"
        f"CANDIDATE RESUME (sole source of truth):\n"
        f"{json.dumps(inputs.resume, indent=2)}\n\n"
        f"WHAT TO EMPHASIZE (from tailoring step):\n"
        f"{json.dumps(inputs.tailoring_plan.get('summary', ''))}; "
        f"high-emphasis skills: "
        f"{json.dumps(inputs.tailoring_plan.get('skills_order', [])[:6])}\n\n"
        "Write the cover letter body now."
    )
    return llm.text(prompt, system=SYSTEM,
                    max_tokens=900, temperature=0.4)


def with_letterhead(body: str, inputs: CoverLetterInputs) -> str:
    """Wrap the body with a standard contact header/footer for export."""
    contact = inputs.resume.get("contact", {})
    name = inputs.resume.get("name", "")
    header_lines = [name] + [str(v) for v in contact.values() if v]
    header = "\n".join(header_lines)
    return f"{header}\n\n{inputs.company}\n\n{body.strip()}\n\nSincerely,\n{name}\n"
