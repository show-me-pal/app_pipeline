"""Step 7 -- autofill standard Workday / Greenhouse application forms.

Maps your structured profile (contact, demographics, links) onto form fields
and uploads the tailored PDF resume. It then STOPS at the review step -- it does
not click final Submit.

Why fill-but-not-submit:
  * Workday and Greenhouse ToS prohibit automated submission, and ATS vendors
    actively flag bot-submitted applications (which can blacklist a candidate).
  * CAPTCHAs exist specifically to block automated submission; defeating them is
    out of scope on purpose.
  * Leaving the final click to you costs ~5 seconds per app and removes the
    catastrophic-mis-parse risk (e.g. a wrong answer to a yes/no eligibility
    question submitted under your name).

To run, you must be authenticated in a persistent browser context (you log in
once; the script reuses the session). Anything ambiguous -> raise
ManualReviewRequired and the loop (step 8) handles it.

Requires: pip install playwright && playwright install chromium
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Optional

from playwright.sync_api import (
    sync_playwright, Page, TimeoutError as PWTimeout, Error as PWError,
)

from .error_handling import ManualReviewRequired, ReviewReason

logger = logging.getLogger("apply.autofill")

# Whether to click the final submit. Default False on purpose. Setting True is
# your decision and your risk; CAPTCHAs/screening questions still divert to
# manual review regardless.
AUTO_SUBMIT = False


@dataclass
class Applicant:
    first_name: str
    last_name: str
    email: str
    phone: str
    location_city: str = ""
    linkedin_url: str = ""
    github_url: str = ""
    portfolio_url: str = ""
    resume_pdf_path: str = ""
    # demographics are optional / often "decline to answer"; map only what you set
    demographics: dict = field(default_factory=dict)


# Field-name patterns -> applicant attribute. Greenhouse uses fairly stable
# label/name attributes; Workday uses automation-id data attributes. We match
# loosely on label text, name, id, and aria-label.
_TEXT_FIELD_MAP = [
    (("first name", "firstname", "legal-name-first"), "first_name"),
    (("last name", "lastname", "legal-name-last"), "last_name"),
    (("email",), "email"),
    (("phone", "mobile"), "phone"),
    (("linkedin",), "linkedin_url"),
    (("github",), "github_url"),
    (("portfolio", "website", "personal site"), "portfolio_url"),
    (("city", "current location"), "location_city"),
]


def _fill_text_fields(page: Page, applicant: Applicant) -> None:
    """Best-effort fill of labeled text inputs. Unmatched required fields are
    NOT guessed; if a required field is left empty we flag for review later."""
    inputs = page.query_selector_all("input[type='text'], input[type='email'], "
                                     "input[type='tel'], input:not([type])")
    for el in inputs:
        descriptor = " ".join(filter(None, [
            (el.get_attribute("name") or ""),
            (el.get_attribute("id") or ""),
            (el.get_attribute("aria-label") or ""),
            (el.get_attribute("placeholder") or ""),
        ])).lower()
        for needles, attr in _TEXT_FIELD_MAP:
            if any(n in descriptor for n in needles):
                value = getattr(applicant, attr, "")
                if value:
                    try:
                        el.fill(value)
                    except PWError:
                        pass  # read-only / hidden; ignore
                break


def _upload_resume(page: Page, applicant: Applicant) -> None:
    if not applicant.resume_pdf_path:
        return
    file_inputs = page.query_selector_all("input[type='file']")
    if not file_inputs:
        raise ManualReviewRequired(
            ReviewReason.UPLOAD_FAILED, "no file input found on page")
    try:
        file_inputs[0].set_input_files(applicant.resume_pdf_path)
    except PWError as exc:
        raise ManualReviewRequired(ReviewReason.UPLOAD_FAILED, str(exc))


def _detect_blockers(page: Page) -> None:
    """Raise ManualReviewRequired for things we refuse to automate."""
    html = page.content().lower()

    # CAPTCHA -- never attempt.
    captcha_markers = ["recaptcha", "g-recaptcha", "hcaptcha", "cf-turnstile",
                       "are you a robot", "i'm not a robot"]
    if any(m in html for m in captcha_markers):
        raise ManualReviewRequired(ReviewReason.CAPTCHA,
                                   "CAPTCHA present; human required")

    # Free-text / unexpected screening questions (custom employer questions).
    # Heuristic: a textarea or a question-like label not in our known set.
    textareas = page.query_selector_all("textarea")
    for ta in textareas:
        label = (ta.get_attribute("aria-label") or ta.get_attribute("name") or "").lower()
        if label and "cover" not in label:  # cover letter handled separately
            raise ManualReviewRequired(
                ReviewReason.SCREENING_QUESTION,
                f"free-text question detected: {label[:120]}")


def _handle_selects(page: Page, applicant: Applicant) -> None:
    """Map known single-selects (e.g. demographic dropdowns we have answers for).
    Unknown multi-selects are explicitly diverted to manual review."""
    # Multi-selects (listbox with aria-multiselectable) are the documented
    # bail-out case: we don't guess combinations.
    multis = page.query_selector_all("[aria-multiselectable='true'], select[multiple]")
    if multis:
        raise ManualReviewRequired(
            ReviewReason.UNMAPPED_MULTISELECT,
            f"{len(multis)} multi-select control(s) require human choice")

    for sel in page.query_selector_all("select:not([multiple])"):
        label = (sel.get_attribute("aria-label") or sel.get_attribute("name") or "").lower()
        answer = None
        for key, val in applicant.demographics.items():
            if key.lower() in label:
                answer = val
                break
        if answer is None:
            continue  # leave unknown selects for the human (don't guess)
        try:
            sel.select_option(label=answer)
        except PWError:
            # Option text mismatch -> let human resolve rather than mis-answer.
            raise ManualReviewRequired(
                ReviewReason.UNKNOWN_FIELD,
                f"could not set select '{label}' to '{answer}'")


def _verify_required_filled(page: Page) -> None:
    """If required fields remain empty after our best effort, divert to review
    rather than submitting an incomplete/incorrect application."""
    required = page.query_selector_all("input[required], select[required], textarea[required]")
    for el in required:
        try:
            val = el.input_value()
        except PWError:
            val = ""
        if not val:
            name = (el.get_attribute("aria-label") or el.get_attribute("name") or "field")
            raise ManualReviewRequired(
                ReviewReason.UNKNOWN_FIELD,
                f"required field left empty: {name[:120]}")


def autofill_application(page: Page, url: str, applicant: Applicant,
                         *, auto_submit: bool = AUTO_SUBMIT) -> None:
    """Fill one application. Raises ManualReviewRequired on anything ambiguous.

    Assumes `page` belongs to an authenticated, persistent context.
    """
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=30000)
    except PWTimeout:
        raise ManualReviewRequired(ReviewReason.TIMEOUT, f"page load timed out: {url}")

    _detect_blockers(page)          # CAPTCHA / free-text questions -> bail
    _fill_text_fields(page, applicant)
    _upload_resume(page, applicant)
    _handle_selects(page, applicant)  # multi-selects -> bail
    _verify_required_filled(page)     # incomplete -> bail

    if auto_submit:
        # Off by default. Even here, we only reach this line if no blocker fired.
        btn = page.query_selector("button[type='submit'], button:has-text('Submit')")
        if btn:
            btn.click()
            logger.info("Submitted application: %s", url)
            return
    logger.info("Filled and ready for your review (not submitted): %s", url)


def make_applicant_runner(applicant: Applicant, *, headless: bool = False,
                          user_data_dir: str = ".browser_profile"):
    """Return an `apply_fn(job)` suitable for run_application_loop (step 8).

    Uses a persistent context so you log in once and the session is reused
    across jobs. headless=False so you can watch / take over.
    """
    def apply_fn(job):
        url = job["url"] if isinstance(job, dict) else getattr(job, "url")
        with sync_playwright() as pw:
            ctx = pw.chromium.launch_persistent_context(
                user_data_dir, headless=headless)
            try:
                page = ctx.new_page()
                autofill_application(page, url, applicant)
                if not headless:
                    # Pause so you can eyeball and click Submit yourself.
                    page.wait_for_timeout(1500)
            finally:
                ctx.close()
    return apply_fn
