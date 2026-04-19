"""
QuestionBlockExtractor: generic DOM scraping for question blocks.
"""

from __future__ import annotations

from typing import List, Optional

from .models import QuestionBlock, Option


async def _safe_text(node) -> str:
    try:
        text = await node.inner_text()
        return (text or "").strip()
    except Exception:
        return ""


class QuestionBlockExtractor:
    async def extract(self, page) -> List[QuestionBlock]:
        blocks: List[QuestionBlock] = []
        try:
            elements = await page.query_selector_all("input:not([type=hidden]), textarea, select")
        except Exception:
            return blocks

        for el in elements:
            try:
                tag = (await el.evaluate("el => el.tagName")).lower()
                etype = (await el.get_attribute("type") or "").lower()
                locator = await self._build_locator(el, tag)
                question_text = await self._nearest_label_text(page, el)
                field_type = self._detect_field_type(tag, etype)
                options = await self._collect_options(el, field_type)
                blocks.append(
                    QuestionBlock(
                        locator=locator,
                        field_type=field_type,
                        question_text=question_text,
                        options=options,
                        multiple=(field_type == "checkbox"),
                    )
                )
            except Exception:
                continue
        return blocks

    async def _build_locator(self, el, tag: str) -> str:
        # Prefer id
        try:
            el_id = await el.get_attribute("id")
            if el_id:
                return f"#{el_id}"
        except Exception:
            pass
        # Fallback to name
        try:
            name = await el.get_attribute("name")
            if name:
                return f"{tag}[name='{name}']"
        except Exception:
            pass
        # Last resort: type selector
        try:
            etype = await el.get_attribute("type")
            if etype:
                return f"{tag}[type='{etype}']"
        except Exception:
            pass
        # If nothing else, try text content (dangerous, but better than empty)
        return tag

    async def _nearest_label_text(self, page, el) -> str:
        # Try explicit <label for="">
        try:
            id_attr = await el.get_attribute("id")
            if id_attr:
                label = await page.query_selector(f"label[for='{id_attr}']")
                if label:
                    txt = await _safe_text(label)
                    if txt:
                        return txt
        except Exception:
            pass

        # Try parent text
        try:
            parent = await el.evaluate_handle("el => el.parentElement")
            if parent:
                txt = await _safe_text(parent)
                if txt and len(txt) <= 200:
                    return txt
        except Exception:
            pass

        # Try previous sibling
        try:
            sib = await el.evaluate_handle("el => el.previousElementSibling")
            if sib:
                txt = await _safe_text(sib)
                if txt:
                    return txt
        except Exception:
            pass

        return ""

    async def _collect_options(self, el, field_type: str) -> Optional[List[Option]]:
        if field_type not in ("select", "radio", "checkbox"):
            return None

        options: List[Option] = []
        try:
            if field_type == "select":
                opts = await el.query_selector_all("option")
                for o in opts:
                    label = await _safe_text(o)
                    loc = o._selector or ""
                    if label:
                        options.append(Option(label=label, locator=loc))
            else:
                # radio/checkbox: find siblings with same name
                name = await el.get_attribute("name")
                if name:
                    group = await el.page.query_selector_all(f"input[name='{name}']")
                else:
                    group = [el]
                for g in group:
                    label = await _safe_text(g)
                    if not label:
                        # try parent/sibling text
                        try:
                            parent = await g.evaluate_handle("el => el.parentElement")
                            label = await _safe_text(parent)
                        except Exception:
                            pass
                    loc = g._selector or ""
                    if label:
                        options.append(Option(label=label, locator=loc))
        except Exception:
            return None

        return options or None

    def _detect_field_type(self, tag: str, etype: str) -> str:
        if tag == "textarea":
            return "textarea"
        if tag == "select":
            return "select"
        if tag == "input":
            if etype in ("radio",):
                return "radio"
            if etype in ("checkbox",):
                return "checkbox"
            if etype in ("file",):
                return "file"
            if etype in ("date",):
                return "date"
            return "text"
        return "text"
