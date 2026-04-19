"""
ATS flows: thin adapters that can override GenericFlow behavior.
"""

from __future__ import annotations

from .analyzer import PageAnalyzer
from .question_extractor import QuestionBlockExtractor
from .form_filler import FormFiller


class BaseFlow:
    ats: str = "generic"

    def __init__(self):
        self.analyzer = PageAnalyzer()
        self.extractor = QuestionBlockExtractor()
        self.filler = FormFiller()

    async def extract_questions(self, page):
        return await self.extractor.extract(page)

    async def page_is_confirmation(self, page) -> bool:
        return await self.analyzer.is_confirmation_page(page)

    async def fill_and_next(self, page, answers):
        raise NotImplementedError


class GenericFlow(BaseFlow):
    ats = "generic"

    async def fill_and_next(self, page, answers):
        # Delegated to NavigationController, so no-op here
        return


class LeverFlow(GenericFlow):
    ats = "lever"

    async def extract_questions(self, page):
        # Lever often keeps everything in a single page/modal; fallback to generic extractor.
        return await super().extract_questions(page)


class GreenhouseFlow(GenericFlow):
    ats = "greenhouse"

    async def extract_questions(self, page):
        # Handle possible iframe embedding
        try:
            iframe_count = await page.locator("iframe").count()
            if iframe_count > 0:
                frame = page.frame_locator("iframe").first
                return await self.extractor.extract(frame)
        except Exception:
            pass
        return await self.extractor.extract(page)


class WorkdayFlow(GenericFlow):
    ats = "workday"

    async def page_is_confirmation(self, page) -> bool:
        html = (await page.content()).lower()
        if "successfully submitted" in html or "you have successfully applied" in html:
            return True
        return await super().page_is_confirmation(page)

    async def extract_questions(self, page):
        # Workday frequently uses iframes; attempt to dive into the first iframe if present.
        try:
            iframe_count = await page.locator("iframe").count()
            if iframe_count > 0:
                frame = page.frame_locator("iframe").first
                return await self.extractor.extract(frame)
        except Exception:
            pass
        return await self.extractor.extract(page)


class AshbyFlow(GenericFlow):
    ats = "ashby"

    async def page_is_confirmation(self, page) -> bool:
        html = (await page.content()).lower()
        if "application submitted" in html or "thank you for applying" in html:
            return True
        return await super().page_is_confirmation(page)

    async def extract_questions(self, page):
        return await super().extract_questions(page)


class SmartRecruitersFlow(GenericFlow):
    ats = "smartrecruiters"

    async def page_is_confirmation(self, page) -> bool:
        html = (await page.content()).lower()
        if "application sent" in html or "thank you for your application" in html:
            return True
        return await super().page_is_confirmation(page)

    async def extract_questions(self, page):
        return await super().extract_questions(page)
