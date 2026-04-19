"""
AnswerEngine: rule-based answers with LLM hooks (placeholders).
"""

from __future__ import annotations

from typing import Any, Tuple

from .models import Profile, QuestionBlock, JobPosting


class AnswerEngine:
    def __init__(self, profile: Profile, config: Any):
        self.profile = profile
        self.config = config

    async def answer(self, qb: QuestionBlock, job: JobPosting) -> Tuple[Any, str]:
        ft = qb.field_type
        qtext = qb.question_text.lower()

        # Text-based
        if ft in ("text", "textarea"):
            rule = self._rule_text(qtext)
            if rule is not None:
                return rule, "rule"
            # Placeholder for LLM
            return self._fallback_text(job, qb), "ai"

        # Select / radio / checkbox
        if ft in ("select", "radio", "checkbox"):
            rule = self._rule_choice(qtext, qb)
            if rule is not None:
                return rule, "rule"
            return self._fallback_choice(qb), "ai"

        # File
        if ft == "file":
            return self._rule_file(qtext), "rule"

        # Date
        if ft == "date":
            return "ASAP", "rule"

        return "", "rule"

    def _rule_text(self, qtext: str):
        parts = self.profile.full_name.split()
        first_name = parts[0] if parts else self.profile.full_name
        last_name = parts[-1] if len(parts) > 1 else ""

        if "first name" in qtext or "given name" in qtext:
            return first_name
        if "last name" in qtext or "family name" in qtext or "surname" in qtext:
            return last_name
        if "name" in qtext:
            return self.profile.full_name
        if "email" in qtext:
            return self.profile.email
        if "phone" in qtext or "telephone" in qtext:
            return self.profile.phone
        if "linkedin" in qtext:
            return self.profile.linkedin_url or ""
        if "github" in qtext:
            return self.profile.github_url or ""
        if "portfolio" in qtext or "website" in qtext:
            return self.profile.portfolio_url or ""
        return None

    def _rule_choice(self, qtext: str, qb: QuestionBlock):
        if "authorized" in qtext or "work in the" in qtext:
            return "Yes" if self.profile.work_auth else "No"
        if "sponsorship" in qtext or "visa" in qtext:
            return "No" if not self.profile.sponsorship_needed else "Yes"
        if "location type" in qtext or "work location" in qtext:
            return self.profile.preferred_location_type
        if "gender" in qtext or "race" in qtext or "ethnicity" in qtext:
            if self.config and getattr(self.config, "prefer_not_to_say", True):
                return "Prefer not to say"
        return None

    def _rule_file(self, qtext: str):
        if "cover" in qtext:
            return self.profile.cover_letter_template_path or self.profile.resume_path
        return self.profile.resume_path

    def _fallback_text(self, job: JobPosting, qb: QuestionBlock) -> str:
        # Placeholder: simple generic answer
        return f"I am interested in the {job.title or 'role'} and believe my background aligns well."

    def _fallback_choice(self, qb: QuestionBlock):
        # Placeholder: pick first option if available
        if qb.options:
            return qb.options[0].label
        return ""
