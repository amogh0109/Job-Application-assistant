"""
BrowserController: Playwright wrapper for page management and helpers.
"""

from __future__ import annotations

from playwright.async_api import async_playwright

_BASE_ARGS = [
    "--disable-gpu",
    "--disable-dev-shm-usage",
    "--no-sandbox",
    "--disable-blink-features=AutomationControlled",
]


class BrowserController:
    def __init__(self, headless: bool = False, context_kwargs: dict | None = None):
        self._p = None
        self._browser = None
        self._context = None
        self.headless = headless
        self.context_kwargs = context_kwargs or {}

    async def __aenter__(self):
        self._p = await async_playwright().start()
        launch_args = list(_BASE_ARGS)
        if self.headless:
            launch_args.append("--headless=new")
        self._browser = await self._p.chromium.launch(headless=self.headless, args=launch_args)
        self._context = await self._browser.new_context(**self.context_kwargs)
        return self

    async def __aexit__(self, *exc):
        if self._context:
            await self._context.close()
        if self._browser:
            await self._browser.close()
        if self._p:
            await self._p.stop()

    async def new_page(self):
        return await self._context.new_page()

    @staticmethod
    async def find_and_click_button_by_text(page, texts: list[str]) -> bool:
        for t in texts:
            try:
                loc = page.get_by_role("button", name=t)
                if await loc.count() > 0:
                    await loc.first.click()
                    return True
            except Exception:
                continue
        return False
