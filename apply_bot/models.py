"""
Core data models for the Apply Bot.
"""

from __future__ import annotations

from typing import Dict, List, Optional
from pydantic import BaseModel


class Profile(BaseModel):
    full_name: str
    email: str
    phone: str
    location: str
    country: Optional[str] = None
    linkedin_url: Optional[str] = None
    github_url: Optional[str] = None
    portfolio_url: Optional[str] = None

    work_auth: bool
    sponsorship_needed: bool
    total_years_experience: float
    years_by_skill: Dict[str, float] = {}

    preferred_location_type: str  # "Remote" | "Hybrid" | "Onsite"
    demographic_preferences: str  # e.g. "prefer_not_to_say"

    resume_path: str
    cover_letter_template_path: Optional[str] = None
    workday_password: Optional[str] = None


class JobPosting(BaseModel):
    url: str
    title: Optional[str] = None
    company: Optional[str] = None
    location: Optional[str] = None
    description_html: Optional[str] = None
    description_text: Optional[str] = None


class Option(BaseModel):
    label: str          # visible text
    locator: str        # CSS/XPath to click/select


class QuestionBlock(BaseModel):
    locator: str        # main input element locator
    field_type: str     # "text", "textarea", "select", "radio", "checkbox", "file", "date"
    question_text: str
    options: Optional[List[Option]] = None
    multiple: bool = False


class ApplicationResult(BaseModel):
    job: JobPosting
    status: str             # "submitted", "review_pending", "failed"
    questions: List[Dict]   # serialized Q/A pairs
    confirmation_text: Optional[str] = None
    error: Optional[str] = None
    timestamp: str
