"""
PageAnalyzer: detect confirmation pages and presence of questions.
"""

from __future__ import annotations

import re


class PageAnalyzer:
    CONFIRM_PATTERNS = [
        re.compile(r"thank you for applying", re.I),
        re.compile(r"application submitted", re.I),
        re.compile(r"we received your application", re.I),
        re.compile(r"thanks for your application", re.I),
        re.compile(r"submission received", re.I),
    ]

    async def is_confirmation_page(self, page) -> bool:
        try:
            html = (await page.content()).lower()
        except Exception:
            return False
        return any(p.search(html) for p in self.CONFIRM_PATTERNS)

    async def has_questions(self, page) -> bool:
        try:
            # Rough heuristic: visible inputs/selects/textareas that are not hidden
            inputs = await page.query_selector_all("input:not([type=hidden]), textarea, select")
            return len(inputs) > 0
        except Exception:
            return False
