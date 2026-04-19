"""
JobContextBuilder: scrape basic job context using the live Playwright page.
"""

from __future__ import annotations

import re
from typing import Optional

from .models import JobPosting


class JobContextBuilder:
    def __init__(self, timeout: int = 8000):
        self.timeout = timeout

    async def build(self, page, url: str) -> JobPosting:
        # Assumes caller already did page.goto(url)
        html = await self._safe_content(page)
        title = await self._extract_title(page, html)
        company = self._extract_company(html)
        location = self._extract_location(html)
        desc_text = self._extract_body_text(html)

        return JobPosting(
            url=url,
            title=title,
            company=company,
            location=location,
            description_html=html,
            description_text=desc_text,
        )

    async def _safe_content(self, page) -> str:
        try:
            return await page.content()
        except Exception:
            return ""

    async def _extract_title(self, page, html: str) -> Optional[str]:
        # Prefer document.title from live page
        try:
            t = await page.title()
            if t:
                return t.strip()
        except Exception:
            pass
        if not html:
            return None
        m = re.search(r"<title>(.*?)</title>", html, re.I | re.S)
        if m:
            return re.sub(r"\s+", " ", m.group(1)).strip()
        m = re.search(r"<h1[^>]*>(.*?)</h1>", html, re.I | re.S)
        if m:
            return re.sub(r"<.*?>", "", m.group(1)).strip()
        return None

    def _extract_company(self, html: str) -> Optional[str]:
        if not html:
            return None
        m = re.search(r'"hiringOrganization"\s*:\s*\{\s*"name"\s*:\s*"([^"]+)"', html, re.I)
        if m:
            return m.group(1).strip()
        m = re.search(r"Company[:\s]+</span>\s*<span[^>]*>([^<]+)", html, re.I)
        if m:
            return m.group(1).strip()
        return None

    def _extract_location(self, html: str) -> Optional[str]:
        if not html:
            return None
        m = re.search(r'"addressLocality"\s*:\s*"([^"]+)"', html, re.I)
        if m:
            return m.group(1).strip()
        m = re.search(r"Location[:\s]+</span>\s*<span[^>]*>([^<]+)", html, re.I)
        if m:
            return m.group(1).strip()
        m = re.search(r"location\"?:\s*\"([^\"]+)\"", html, re.I)
        if m:
            return m.group(1).strip()
        return None

    def _extract_body_text(self, html: str) -> Optional[str]:
        if not html:
            return None
        text = re.sub(r"<script.*?</script>", " ", html, flags=re.S | re.I)
        text = re.sub(r"<style.*?</style>", " ", text, flags=re.S | re.I)
        text = re.sub(r"<[^>]+>", " ", text)
        text = re.sub(r"\s+", " ", text)
        return text.strip() if text else None
