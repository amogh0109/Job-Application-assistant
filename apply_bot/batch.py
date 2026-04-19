"""
BatchOrchestrator: runs multiple job applications in sequence.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, List

import asyncio
from .models import ApplicationResult, JobPosting, Profile
from .navigation import NavigationController
from .logger import RunLogger
from .job_context import JobContextBuilder
from .browser import BrowserController


class BatchOrchestrator:
    def __init__(self, profile: Profile, config: Any, logger: RunLogger, context_builder: JobContextBuilder):
        self.profile = profile
        self.config = config
        self.logger = logger
        self.context_builder = context_builder

    async def run(self, job_links: List[str]) -> List[ApplicationResult]:
        results: List[ApplicationResult] = []
        async with BrowserController(headless=getattr(self.config, "headless", False)) as browser:
            for url in job_links[: self.config.max_jobs_per_run]:
                page = await browser.new_page()
                try:
                    await page.goto(url)
                    job = await self.context_builder.build(page, url)
                    nav = NavigationController(self.profile, self.config, logger=self.logger)
                    result = await nav.run(page, job)
                except Exception as e:
                    job = JobPosting(url=url)
                    result = ApplicationResult(
                        job=job,
                        status="failed",
                        questions=[],
                        confirmation_text=None,
                        error=str(e),
                        timestamp=datetime.now(timezone.utc).isoformat(),
                    )
                if getattr(self.config, "mode", "auto_submit") == "review":
                    print(f"\n[REVIEW MODE] Application filled for: {url}")
                    print("Please review the browser window. Press Enter in this terminal when you are done to close the tab...")
                    await asyncio.to_thread(input)

                await page.close()
                summary = self._sanitize(result)
                self.logger.log(summary)
                try:
                    print(f"[{summary.get('status')}] {summary.get('job_url')} | error={summary.get('error')}")
                except Exception:
                    pass
                results.append(result)
        return results

    def _sanitize(self, result: ApplicationResult) -> dict:
        return {
            "job_url": result.job.url,
            "status": result.status,
            "error": result.error,
            "timestamp": result.timestamp,
        }
