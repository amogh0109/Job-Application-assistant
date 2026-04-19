"""
FormFiller: write answers to the page based on QuestionBlock definitions.
"""

from __future__ import annotations

from typing import Any, List

from .models import QuestionBlock


class FormFiller:
    async def fill_block(self, page, qb: QuestionBlock, answer: Any) -> None:
        loc = page.locator(qb.locator)
        if qb.field_type in ("text", "textarea", "date"):
            await loc.fill(str(answer))
            return

        if qb.field_type == "select":
            try:
                await loc.select_option(label=str(answer))
            except Exception:
                await loc.fill(str(answer))
            return

        if qb.field_type == "radio":
            # answer is label text
            try:
                await page.get_by_label(str(answer), exact=False).check()
                return
            except Exception:
                pass
            await loc.check(force=True)
            return

        if qb.field_type == "checkbox":
            # answer may be bool or list
            if isinstance(answer, list):
                for a in answer:
                    try:
                        await page.get_by_label(str(a), exact=False).check()
                    except Exception:
                        pass
            else:
                if answer:
                    await loc.check(force=True)
            return

        if qb.field_type == "file":
            await loc.set_input_files(str(answer))
            return

    async def fill_all(self, page, qbs: List[QuestionBlock], answers: dict) -> None:
        for qb in qbs:
            ans = answers.get(qb.locator)
            if ans is None:
                continue
            await self.fill_block(page, qb, ans)
