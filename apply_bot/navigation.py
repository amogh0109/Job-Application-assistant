"""
NavigationController: orchestrate extraction, answering, filling, and advancement.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional
from datetime import datetime, timezone
from urllib.parse import urlparse
from pathlib import Path
import asyncio
import os
import re
import time

try:
    import google.generativeai as genai  # type: ignore
    _HAS_GEMINI = True
except Exception:
    genai = None
    _HAS_GEMINI = False
from .analyzer import PageAnalyzer
from .question_extractor import QuestionBlockExtractor
from .form_filler import FormFiller
from .answer_engine import AnswerEngine
from .ats_registry import get_flow
from .models import ApplicationResult, JobPosting, QuestionBlock, Option
from .gemini_analyzer import analyze_html
from .gemini_planner import plan_actions
from .email_client import fetch_greenhouse_code

NEXT_BUTTON_TEXTS = [
    "Next",
    "Continue",
    "Save and continue",
    "Review",
    "Submit",
    "Apply",
    "Save & Next",
]
SUBMIT_BUTTON_TEXTS = [
    "Submit",
    "Submit Application",
    "Apply",
    "Apply for this job",
    "Finish",
    "Complete",
    "Send",
]


class NavigationController:
    def __init__(self, profile, config, logger=None):
        self.profile = profile
        self.config = config
        self.logger = logger
        self.debug_dir = Path(getattr(config, "log_dir", "out/logs")) / "debug"
        self.debug_dir.mkdir(parents=True, exist_ok=True)
        self.analyzer = PageAnalyzer()
        self.extractor = QuestionBlockExtractor()
        self.filler = FormFiller()
        self._apply_clicked = False
        self._debug_enabled = True
        self._form_visible = False
        self._action_history: List[str] = []
        self._last_signature: str = ""
        # Phase-based filling strategy
        self._current_phase = 0  # 0=text, 1=dropdowns, 2=checkboxes, 3=files, 4=submit
        self._phase_attempts = 0  # Track attempts in current phase
        self._max_phase_attempts = 3  # Max attempts before moving to next phase
        self._submit_pending = False
        self._submit_started_at: Optional[float] = None
        self._submit_started_utc: Optional[datetime] = None
        self._submit_timeout_sec = getattr(config, "submit_wait_seconds", 120)
        self._resume_prepass_done = False
        self._otp_last_attempt_utc: Optional[datetime] = None
        self._otp_last_code: Optional[str] = None
        self._dbg("NavigationController initialized")

    async def _form_present(self, page) -> bool:
        try:
            # Check for common form signatures
            # 1. Standard Apply forms (Greenhouse, Lever, etc often use #application-form or similar)
            if await page.locator("#application-form, form[action*='/applications'], form[action*='/jobs']").count() > 0:
                return True
            
            # 2. Look for inputs with specific profile-related IDs or attributes
            inputs = page.locator("input[id='first_name'], input[id='last_name'], input[id='email'], input[name*='first_name']")
            count = await inputs.count()
            for i in range(count):
                if await inputs.nth(i).is_visible():
                    return True
                
            # 3. Fallback: Generic input search (weaker signal)
            loc = page.locator("input[type='text'], input[type='email']")
            count = await loc.count()
            visible_count = 0
            for i in range(min(count, 5)):
                 if await loc.nth(i).is_visible():
                      visible_count += 1
            # If we see 3+ visible text inputs, it's likely a form
            return visible_count >= 3
        except Exception:
            return False

    async def run(self, page, job: JobPosting) -> ApplicationResult:
        ats = self._guess_ats_from_url(job.url)
        flow = get_flow(ats)
        answers_log: List[Dict[str, Any]] = []

        self._dbg(f"Start job: {job.url} | ATS={ats}")
        
        # Prevent native file pickers from blocking the bot
        async def handle_file_chooser(file_chooser):
            self._dbg("Intercepted native file chooser! Attempting to resolve...")
            try:
                path = None
                el = None
                try:
                    el = file_chooser.element()
                    if el is not None and hasattr(el, "__await__"):
                        el = await el
                except Exception:
                    el = None
                if el:
                    try:
                        el_id = await el.get_attribute("id") or ""
                        el_name = await el.get_attribute("name") or ""
                        el_label = await el.evaluate(
                            "(node) => node.getAttribute('aria-label') || (node.labels && node.labels[0] ? node.labels[0].innerText : '') || ''"
                        )
                        key = f"{el_id} {el_name} {el_label}".lower()
                        cover = getattr(self.profile, "cover_letter_template_path", "") or getattr(self.profile, "cover_letter_path", "")
                        resume = getattr(self.profile, "resume_path", "")
                        if "cover" in key:
                            path = cover or None
                        elif "resume" in key or "cv" in key:
                            path = resume or None
                        else:
                            path = resume or cover or None
                    except Exception:
                        path = None

                if path and os.path.exists(path):
                    self._dbg(f"Automatically providing file to native chooser: {path}")
                    await file_chooser.set_files(path)
                else:
                    self._dbg("No suitable file found, cancelling native chooser.")
                    await file_chooser.set_files([])
            except Exception as e:
                self._dbg(f"File chooser resolution failed: {e}")

        page.on("filechooser", handle_file_chooser)

        await page.wait_for_load_state("domcontentloaded")
        await self._ensure_application_view(page, ats)

        last_action_was_submit = False
        while True:
            self._form_visible = self._form_visible or await self._form_present(page)
            
            if last_action_was_submit:
                self._dbg("Last action was SUBMIT. Waiting for state transition...")
                await page.wait_for_timeout(5000)
                last_action_was_submit = False

            if self._submit_pending:
                if await flow.page_is_confirmation(page):
                    confirmation_text = await self._extract_confirmation_text(page)
                    return self._result(job, "submitted", answers_log, confirmation_text, None)
                if ats == "greenhouse" and await self._is_greenhouse_verification(page):
                    self._dbg("Post-submit verification detected. Handling OTP...")
                    if await self._handle_verification(page):
                        self._register_submit_attempt()
                        await page.wait_for_timeout(1000)
                        continue
                    if not self._submit_wait_exceeded():
                        self._dbg("OTP handling failed; retrying while submit pending.")
                        await page.wait_for_timeout(2000)
                        continue
                    return self._result(job, "failed", answers_log, None, "verification-failed")
                if await self._has_validation_errors(page):
                    self._dbg("Validation errors detected after submit; resuming form fill.")
                    self._clear_submit_pending("validation errors")
                else:
                    if not self._submit_wait_exceeded():
                        await page.wait_for_timeout(2000)
                        continue
                    self._dbg("Submit wait exceeded; resuming form fill.")
                    self._clear_submit_pending("timeout")

            if self._form_visible and not self._resume_prepass_done:
                await self._attempt_resume_prepass(page)

            if ats == "greenhouse":
                if await self._is_greenhouse_verification(page):
                    self._dbg("Greenhouse verification detected (heuristic). Handling OTP...")
                    if await self._handle_verification(page):
                        self._register_submit_attempt()
                        continue
                    if self._submit_pending and not self._submit_wait_exceeded():
                        self._dbg("OTP handling failed; retrying while submit pending.")
                        await page.wait_for_timeout(2000)
                        continue
                    return self._result(job, "failed", answers_log, None, "verification-failed")

            if getattr(self.config, "gemini_api_key", ""):
                try:
                    await page.wait_for_load_state("networkidle", timeout=3000)
                except Exception:
                    pass

                # _plan_and_execute now returns (state, did_submit)
                res = await self._plan_and_execute(page)
                if not res:
                    pass # fallback to heuristics
                else:
                    planned_state, did_submit = res
                    last_action_was_submit = did_submit
                    self._dbg(f"Planner state: {planned_state} | did_submit: {did_submit}")
                    
                    if planned_state == "confirmation":
                        if ats == "greenhouse" and await self._is_greenhouse_verification(page):
                            self._dbg("Verification screen detected (planner confirmation). Handling OTP...")
                            if await self._handle_verification(page):
                                self._register_submit_attempt()
                                await page.wait_for_timeout(1000)
                                continue
                            if self._submit_pending and not self._submit_wait_exceeded():
                                await page.wait_for_timeout(2000)
                                continue
                            return self._result(job, "failed", answers_log, None, "verification-failed")
                        if await flow.page_is_confirmation(page):
                            confirmation_text = await self._extract_confirmation_text(page)
                            return self._result(job, "submitted", answers_log, confirmation_text, None)
                        if await self._perform_final_check(page):
                            if not self._submit_pending and await self._click_submit(page):
                                await page.wait_for_timeout(1000)
                                continue
                            self._dbg("Planner reported confirmation, but no confirmation detected yet.")
                        else:
                            self._dbg("Final check found missing fields. Continuing filling...")
                        continue # Re-plan

                    if planned_state == "verification":
                        self._dbg("Verification screen detected. Handling OTP...")
                        if await self._handle_verification(page):
                            self._register_submit_attempt()
                            await page.wait_for_timeout(1000)
                            continue # Re-plan after verification
                        if self._submit_pending and not self._submit_wait_exceeded():
                            self._dbg("OTP handling failed; retrying while submit pending.")
                            await page.wait_for_timeout(2000)
                            continue
                        return self._result(job, "failed", answers_log, None, "verification-failed")

                    if planned_state == "blocked":
                         await self._capture_debug(page, job.url, reason="planner-blocked")
                         return self._result(job, "failed", answers_log, None, "planner-blocked")

                    if planned_state == "stuck_in_loop":
                         if self._submit_pending and not self._submit_wait_exceeded():
                             self._dbg("Planner loop detected during submit wait; continuing.")
                             await page.wait_for_timeout(2000)
                             continue
                         await self._capture_debug(page, job.url, reason="planner-loop")
                         return self._result(job, "failed", answers_log, None, "planner-loop")

                    if planned_state in ("review", "review_mode"):
                        return self._result(job, "review_pending", answers_log, None, None)

                    # If planner ran, we generally want to loop again or continue
                    continue

            if not self._apply_clicked:
                await self._try_initial_apply(page)
                self._apply_clicked = True
                # let dynamic forms render
                try:
                    await page.wait_for_load_state("networkidle", timeout=5000)
                except Exception:
                    pass
                await page.wait_for_timeout(1000)

            # Workday account creation gate: stop and report
            if ats == "workday" and await self._is_workday_account_creation(page):
                signed_in = await self._handle_workday_signin(page)
                self._dbg(f"Workday gate: sign-in attempted, success={signed_in}")
                if signed_in:
                    await page.wait_for_timeout(1000)
                else:
                    filled = await self._handle_workday_account_creation(page)
                    self._dbg(f"Workday gate: account creation attempted, success={filled}")
                    if not filled:
                        await self._capture_debug(page, job.url, reason="workday-account-creation")
                        return self._result(job, "failed", answers_log, None, "workday-account-creation-required")
                    # allow next loop to extract after account creation
                    await page.wait_for_timeout(1000)

            if await flow.page_is_confirmation(page):
                confirmation_text = await self._extract_confirmation_text(page)
                return self._result(job, "submitted", answers_log, confirmation_text, None)

            try:
                qbs = await flow.extract_questions(page)
                if not qbs:
                    # try iframe if present
                    try:
                        iframe_count = await page.locator("iframe").count()
                        if iframe_count > 0:
                            for fr in page.frames:
                                if fr == page.main_frame:
                                    continue
                                qbs = await self.extractor.extract(fr)
                                if qbs:
                                    break
                    except Exception:
                        pass
                if not qbs:
                    # wait a bit and retry once
                    await page.wait_for_timeout(1500)
                    qbs = await flow.extract_questions(page)
                if not qbs:
                    await page.wait_for_timeout(1000)
                    qbs = await flow.extract_questions(page)
                if not qbs and getattr(self.config, "gemini_api_key", ""):
                    gemini_qbs = await self._gemini_extract(page)
                    if gemini_qbs:
                        qbs = gemini_qbs
                self._dbg(f"Heuristic extraction found {len(qbs) if qbs else 0} fields")
            except Exception as e:
                self._dbg(f"Extract-error: {e}")
                return self._result(job, "failed", answers_log, None, f"extract-error: {e}")

            if not qbs and await self.analyzer.has_questions(page) is False:
                # nothing to do; attempt submit click if allowed
                await self._capture_debug(page, job.url, reason="no-questions")
                if self.config.mode == "auto_submit":
                    clicked = await self._click_next(page)
                    if not clicked:
                        return self._result(job, "failed", answers_log, None, "no-questions-no-next")
                    continue
                else:
                    return self._result(job, "failed", answers_log, None, "no-questions-detected")

            answers = {}
            for qb in qbs:
                ans, source = await self._answer(qb, job)
                answers[qb.locator] = ans
                answers_log.append(
                    {
                        "text": qb.question_text,
                        "field_type": qb.field_type,
                        "answer": ans,
                        "options": [o.label for o in qb.options] if qb.options else None,
                        "source": source,
                    }
                )

            await self.filler.fill_all(page, qbs, answers)

            if self.config.mode == "review":
                return self._result(job, "review_pending", answers_log, None, None)

            clicked = await self._click_next(page)
            if not clicked:
                return self._result(job, "failed", answers_log, None, "next-button-not-found")

    async def _click_next(self, page) -> bool:
        for t in NEXT_BUTTON_TEXTS:
            try:
                loc = page.get_by_role("button", name=t)
                if await loc.count() > 0:
                    await loc.first.click()
                    return True
            except Exception:
                continue
        # fallback: submit inputs
        try:
            loc = page.locator("input[type='submit']")
            if await loc.count() > 0:
                await loc.first.click()
                return True
        except Exception:
            pass
        return False

    async def _click_submit(self, page) -> bool:
        for t in SUBMIT_BUTTON_TEXTS:
            try:
                loc = page.get_by_role("button", name=t)
                if await loc.count() > 0 and await loc.first.is_enabled():
                    await loc.first.click()
                    self._register_submit_attempt()
                    return True
            except Exception:
                continue
        # fallback: submit inputs
        try:
            loc = page.locator("input[type='submit']")
            if await loc.count() > 0 and await loc.first.is_enabled():
                await loc.first.click()
                self._register_submit_attempt()
                return True
        except Exception:
            pass
        return False

    async def _answer(self, qb, job):
        engine = AnswerEngine(self.profile, self.config)
        return await engine.answer(qb, job)

    def _result(self, job, status, questions, confirmation_text, error):
        return ApplicationResult(
            job=job,
            status=status,
            questions=questions,
            confirmation_text=confirmation_text,
            error=error,
            timestamp=datetime.now(timezone.utc).isoformat(),
        )

    async def _extract_confirmation_text(self, page) -> str:
        try:
            return (await page.text_content("body")) or ""
        except Exception:
            return ""

    def _guess_ats_from_url(self, url: str) -> str:
        host = (urlparse(url).hostname or "").lower()
        if "lever.co" in host:
            return "lever"
        if "greenhouse.io" in host or "boards.greenhouse.io" in host:
            return "greenhouse"
        if "myworkdayjobs" in host or "wd1.myworkdayjobs" in host:
            return "workday"
        if "ashbyhq.com" in host:
            return "ashby"
        if "smartrecruiters.com" in host:
            return "smartrecruiters"
        return "generic"

    async def _ensure_application_view(self, page, ats: str) -> None:
        # Try ATS-specific apply triggers before extraction loop
        self._dbg(f"Ensure application view for ATS={ats}")
        if ats == "workday":
            await self._try_workday_apply(page)
            await self._handle_workday_modal(page)
        elif ats == "greenhouse":
            await self._try_greenhouse_apply(page)
        # generic wait for inputs
        await self._wait_for_inputs(page)

    async def _try_initial_apply(self, page) -> None:
        apply_texts = [
            "Apply",
            "Apply now",
            "Apply Now",
            "Apply for this job",
            "Apply for this position",
        ]
        for t in apply_texts:
            try:
                loc = page.get_by_role("button", name=t)
                if await loc.count() > 0:
                    await loc.first.click()
                    return
                loc = page.get_by_role("link", name=t)
                if await loc.count() > 0:
                    await loc.first.click()
                    return
            except Exception:
                continue
        # Fallback: common selectors
        selectors = [
            "button:has-text('Apply')",
            "a:has-text('Apply')",
            "button[data-qa='apply-button']",
            "button[data-testid='apply-button']",
        ]
        for sel in selectors:
            try:
                loc = page.locator(sel)
                if await loc.count() > 0:
                    await loc.first.click()
                    return
            except Exception:
                continue

    async def _handle_workday_modal(self, page) -> None:
        # Handle Workday "Start Your Application" modal by clicking a primary action
        selectors = [
            "button:has-text('Apply Manually')",
            "button:has-text('Autofill with Resume')",
            "button[data-automation-id='applyManuallyButton']",
            "button[data-automation-id='autofillWithResumeButton']",
        ]
        for sel in selectors:
            try:
                loc = page.locator(sel)
                if await loc.count() > 0 and await loc.first.is_enabled():
                    await loc.first.click()
                    await page.wait_for_timeout(1500)
                    return
            except Exception:
                continue
        # some sites may disable buttons; try to close if not actionable
        try:
            close_btn = page.locator("button[aria-label='Close'], button:has-text('Close')")
            if await close_btn.count() > 0:
                await close_btn.first.click()
        except Exception:
            pass

    async def _try_workday_apply(self, page) -> None:
        # If already on apply page, return
        if "/apply" in page.url:
            return
        # Accept cookies if banner is present
        try:
            cookie_btn = page.locator("button[data-automation-id='legalNoticeAcceptButton']")
            if await cookie_btn.count() > 0:
                await cookie_btn.first.click()
                await page.wait_for_timeout(500)
        except Exception:
            pass
        selectors = [
            "a[data-automation-id='adventureButton']",
            "a[href*='/apply']",
            "button[data-automation-id='applyButton']",
            "button[data-automation-id='applyNowButton']",
            "button:has-text('Apply')",
            "a:has-text('Apply')",
        ]
        for sel in selectors:
            try:
                loc = page.locator(sel)
                if await loc.count() > 0:
                    href = await loc.first.get_attribute("href")
                    await loc.first.scroll_into_view_if_needed()
                    try:
                        async with page.expect_navigation(wait_until="domcontentloaded", timeout=8000):
                            await loc.first.click()
                    except Exception:
                        # If navigation didn't happen, try direct goto
                        pass
                    # If href is present and "/apply" is in it, and current URL doesn't have it, force goto
                    if href and "/apply" in href and "/apply" not in page.url:
                        from urllib.parse import urljoin
                        target = urljoin(page.url, href)
                        try:
                            await page.goto(target)
                        except Exception:
                            pass
                    await page.wait_for_timeout(1500)
                    return
            except Exception:
                continue

    async def _try_greenhouse_apply(self, page) -> None:
        if "#app" in page.url or "/applications" in page.url:
            return
        selectors = [
            "a#apply",
            "a[href='#app']",
            "a:has-text('Apply for this job')",
            "button:has-text('Apply for this job')",
            "a:has-text('Apply Now')",
            "button:has-text('Apply Now')",
            "a:has-text('Apply')",
            "button:has-text('Apply')",
        ]
        for sel in selectors:
            try:
                loc = page.locator(sel)
                if await loc.count() > 0:
                    await loc.first.scroll_into_view_if_needed()
                    await loc.first.click()
                    await page.wait_for_timeout(1000)
                    # wait for form inputs to appear
                    try:
                        await page.wait_for_selector("form[action*='/applications'] input, #app input, input[name*='first' i]", timeout=6000, state="visible")
                        self._form_visible = True
                    except Exception:
                        pass
                    return
            except Exception:
                continue

    async def _wait_for_inputs(self, page) -> None:
        try:
            await page.wait_for_selector("input:not([type=hidden]), textarea, select", timeout=8000)
            return
        except Exception:
            pass
        # try iframe
        try:
            iframe_count = await page.locator("iframe").count()
            if iframe_count > 0:
                for fr in page.frames:
                    if fr == page.main_frame:
                        continue
                    try:
                        await fr.wait_for_selector("input:not([type=hidden]), textarea, select", timeout=8000)
                        return
                    except Exception:
                        continue
        except Exception:
            pass


    async def _perform_final_check(self, page) -> bool:
        """
        One last aggressive scan to ensure nothing was missed.
        Returns True if the form appears solid, False if additions are needed.
        """
        self._dbg("Running Final Form Audit...")
        if not _HAS_GEMINI or not getattr(self.config, "gemini_api_key", ""):
            self._dbg("Final audit skipped: Gemini unavailable or missing API key.")
            return False
        try:
            html = await self._get_page_content_with_frames(page)
            audit_prompt = (
                "ACT AS A QUALITY ASSURANCE AGENT. Review this application page HTML.\n"
                "Are there any input fields, dropdowns, or checkboxes that are visible but NOT filled/selected?\n"
                "Pay special attention to 'required' attributes or error messages.\n"
                "If EVERY QUESTION IS ANSWERED, return exactly: {\"ready\": true}\n"
                "If ANYTHING IS MISSING, return exactly: {\"ready\": false}\n"
                "Return JSON ONLY. No prose."
            )
            
            genai.configure(api_key=getattr(self.config, "gemini_api_key", ""))
            model = genai.GenerativeModel(getattr(self.config, "gemini_model", "models/gemini-2.0-flash-lite"))
            resp = await asyncio.to_thread(model.generate_content, f"{audit_prompt}\nHTML:\n{html[:300000]}")
            
            txt = resp.text or ""
            if "true" in txt.lower():
                self._dbg("Final Audit passed: Form is 100% complete.")
                return True
            else:
                self._dbg("Final Audit failed: Missing fields detected.")
                return False
        except Exception as e:
            self._dbg(f"Audit error (skipping): {e}")
            return False

    async def _plan_and_execute(self, page) -> Optional[str]:
        """
        Call Gemini planner and execute proposed actions. Returns planner state if available.
        """
        try:
            html = await self._get_page_content_with_frames(page)
        except Exception:
            return None
        if self._debug_enabled:
            try:
                snap = (html or "")[:2000]
                self._dbg(f"Planner HTML snapshot (first 2000 chars):\n{snap}")
            except Exception:
                pass
        data = plan_actions(
            html, 
            getattr(self.config, "gemini_api_key", ""), 
            getattr(self.config, "gemini_model", "models/gemini-2.5-flash"),
            phase=self._current_phase,
            logger=self.logger
        )
        if not data:
            return None
        state = data.get("state")
        actions = data.get("actions") or []
        actions = self._normalize_planner_actions(actions)
        actions = await self._filter_optional_file_actions(page, actions)
        
        # Phase progression logic (MUST run before loop detection)
        if len(actions) == 0:
            self._phase_attempts += 1
            self._dbg(f"Phase {self._current_phase} returned 0 actions. Attempt {self._phase_attempts}/{self._max_phase_attempts}")
            if self._phase_attempts >= self._max_phase_attempts:
                if self._current_phase < 4:
                    self._current_phase += 1
                    self._phase_attempts = 0
                    self._dbg(f"Moving to next phase: {self._current_phase}")
                    # Re-plan with new phase
                    return await self._plan_and_execute(page)
                else:
                    # We're at submit phase with no actions - form might be complete
                    self._dbg("Submit phase with no actions. Attempting manual submit detection.")
                    if await self._click_submit(page):
                        return state, True
            # Skip loop detection when in phase progression mode
            # Return early to avoid false positive loop detection on empty actions
            return state, False
        else:
            # Reset attempts counter when we get actions
            self._phase_attempts = 0
        
        # Custom Loop Detection: hash current state + actions
        action_strs = [
            f"{a.get('action')}:{a.get('target') or a.get('target_text') or a.get('field') or a.get('field_label')}:{a.get('value') or a.get('option_label') or ''}"
            for a in actions
        ]
        current_sig = f"{state}|" + "|".join(action_strs)
        
        if current_sig == self._last_signature:
            self._dbg("STRICT LOOP DETECTED: Planner produced identical plan to previous turn. Aborting Turn.")
            return "stuck_in_loop", False
        
        self._last_signature = current_sig

        # Existing history-based detection
        if actions:
            # We focus on the first action as the primary driver
            primary_act = actions[0]
            target_val = primary_act.get("target") or primary_act.get("target_text") or primary_act.get("field") or primary_act.get("target_selector") or primary_act.get("field_label")
            value_val = primary_act.get("value") or primary_act.get("option_label") or ""
            act_sig = f"{primary_act.get('action')}:{target_val}:{value_val}"
            
            # Check if we've done this exact action recently
            recent_matches = [x for x in self._action_history[-5:] if x == act_sig]
            if len(recent_matches) >= 3:
                self._dbg(f"LOOP DETECTED: Action '{act_sig}' repeated 3+ times. Skipping/Aborting.")
                return "stuck_in_loop", False
                
            self._action_history.append(act_sig)
            if len(self._action_history) > 20: 
                self._action_history.pop(0)

        self._dbg(f"Planner returned state={state}, actions={len(actions)}")
        initial_url = page.url
        did_submit = False
        for act in actions:
            if act.get("action") == "click":
                target = str(act.get("target") or "").lower()
                target_text = str(act.get("target_text") or act.get("field_label") or "").lower()
                if any(k in target for k in ["submit", "send", "apply"]) or any(k in target_text for k in ["submit", "send", "apply"]):
                    did_submit = True
            
            await self._execute_action(page, act)
            # If URL changed significantly, stop and re-plan
            if page.url != initial_url:
                break
            await page.wait_for_timeout(500)
        return state, did_submit


    async def _is_greenhouse_verification(self, page) -> bool:
        keywords = [
            "verification code",
            "enter the code",
            "check your email",
            "verify your email",
            "one-time code",
            "security code",
        ]
        if await self._has_verification_inputs(page):
            # High confidence if multiple OTP inputs are visible
            if await self._page_has_keywords(page, keywords):
                return True
            return True
        if await self._page_has_keywords(page, keywords):
            return True
        return False

    async def _page_has_keywords(self, page, keywords: List[str]) -> bool:
        try:
            html = (await page.content()).lower()
        except Exception:
            html = ""
        if any(k in html for k in keywords):
            return True
        try:
            for fr in page.frames:
                if fr == page.main_frame or fr.is_detached():
                    continue
                try:
                    f_html = (await fr.content()).lower()
                    if any(k in f_html for k in keywords):
                        return True
                except Exception:
                    continue
        except Exception:
            pass
        return False

    async def _has_verification_inputs(self, page) -> bool:
        loc = await self._find_verification_inputs(page)
        if not loc:
            return False
        try:
            count = await loc.count()
            if count >= 4:
                return True
            auto = await loc.first.get_attribute("autocomplete")
            if auto and "one-time-code" in auto:
                return True
        except Exception:
            pass
        return False

    async def _has_otp_error(self, page) -> bool:
        keywords = [
            "incorrect security code",
            "invalid security code",
            "incorrect code",
            "invalid code",
            "code is incorrect",
        ]
        if await self._page_has_keywords(page, keywords):
            return True
        return False

    async def _find_verification_inputs(self, page):
        selectors = [
            "input[autocomplete='one-time-code']",
            "input[maxlength='1']",
            "input[aria-label*='digit' i]",
            "input[id*='code' i]",
            "input[name*='code' i]",
            "input[id*='otp' i]",
            "input[name*='otp' i]",
        ]
        try:
            contexts = [page] + [f for f in page.frames if f != page.main_frame and not f.is_detached()]
        except Exception:
            contexts = [page]
        for ctx in contexts:
            for sel in selectors:
                try:
                    loc = ctx.locator(sel).filter(visible=True)
                    if await loc.count() > 0:
                        return loc
                except Exception:
                    continue
        return None

    async def _click_verification_submit(self, page) -> bool:
        submit_texts = ["Submit", "Verify", "Continue", "Confirm", "Finish"]
        try:
            contexts = [page] + [f for f in page.frames if f != page.main_frame and not f.is_detached()]
        except Exception:
            contexts = [page]
        for ctx in contexts:
            for t in submit_texts:
                try:
                    loc = ctx.get_by_role("button", name=t).filter(visible=True)
                    if await loc.count() > 0:
                        await loc.first.click()
                        return True
                except Exception:
                    continue
            try:
                fallback = ctx.locator("button:has-text('Submit'), button[id*='submit'], input[type='submit']").filter(visible=True)
                if await fallback.count() > 0:
                    await fallback.first.click()
                    return True
            except Exception:
                continue
        return False

    async def _handle_verification(self, page) -> bool:
        user = getattr(self.config, "email_user", "")
        pwd = getattr(self.config, "email_app_password", "")
        if not user or not pwd:
            self._dbg("Email credentials missing in config. Skipping automation.")
            return False

        max_attempts = 3
        for attempt in range(1, max_attempts + 1):
            after_time = self._submit_started_utc
            if self._otp_last_attempt_utc and after_time:
                after_time = max(after_time, self._otp_last_attempt_utc)
            elif self._otp_last_attempt_utc:
                after_time = self._otp_last_attempt_utc

            self._dbg(f"Polling inbox ({user}) for Greenhouse code... (attempt {attempt}/{max_attempts})")
            code = await asyncio.to_thread(fetch_greenhouse_code, user, pwd, 90, after_time)
            if not code:
                self._dbg("Failed to fetch verification code from email.")
                return False
            if self._otp_last_code and code == self._otp_last_code:
                self._dbg("Fetched OTP matches previous attempt; waiting for a newer code.")
                self._otp_last_attempt_utc = datetime.now(timezone.utc)
                await page.wait_for_timeout(2000)
                continue

            self._otp_last_code = code
            self._otp_last_attempt_utc = datetime.now(timezone.utc)

            self._dbg(f"Verification code fetched: {code}. Injecting...")
            try:
                inputs = await self._find_verification_inputs(page)
                if inputs:
                    count = await inputs.count()
                    is_multi = False
                    try:
                        max_len = await inputs.first.get_attribute("maxlength")
                        is_multi = max_len == "1" or count >= len(code)
                    except Exception:
                        is_multi = count >= len(code)

                    if is_multi and count >= len(code):
                        for i in range(count):
                            await inputs.nth(i).fill("")
                        for i in range(len(code)):
                            await inputs.nth(i).fill(code[i])
                    else:
                        await inputs.first.fill("")
                        await inputs.first.fill(code)
                else:
                    await page.keyboard.type(code)

                await page.wait_for_timeout(1000)
                if await self._click_verification_submit(page):
                    await page.wait_for_timeout(2000)

                if await self._has_otp_error(page):
                    self._dbg("OTP error detected after submit; re-polling for a newer code.")
                    self._otp_last_attempt_utc = datetime.now(timezone.utc)
                    continue

                return True
            except Exception as e:
                self._dbg(f"Error during code injection: {e}")
                return False
        return False


    async def _execute_action(self, page, act: Dict[str, Any]) -> None:
        action = (act.get("action") or "").lower()
        target_selector = act.get("target_selector")
        target = act.get("target") or act.get("id") or act.get("field")
        target_text = act.get("target_text") or act.get("field_label") or act.get("label")
        if not target_text and target and not target.startswith((".", "#", "[")):
            target_text = target
        value = act.get("value")
        option_label = act.get("option_label") or value
        
        # map special tokens
        value = self._map_token(value)
        
        if not target_selector and target:
            if target.startswith((".", "#", "[")):
                target_selector = target
            else:
                # Treat as ID or Name
                target_selector = f"#{target}, [name='{target}'], [id='{target}']"

        field_label = target_text
        try:
            if action in ("verification_code", "otp", "verification"):
                self._dbg("Action verification_code received. Attempting OTP handling...")
                success = await self._handle_verification(page)
                if not success:
                    self._dbg("OTP handling failed for verification_code action.")
                return
            if action == "click":
                loc = await self._find_locator(page, target_selector, target_text, role_button=True)
                if not loc:
                    self._dbg(f"Action click target NOT FOUND: text={target_text} selector={target_selector}")
                    return

                # Filter for visible only
                loc = loc.filter(visible=True).first
                if await loc.count() == 0:
                    self._dbg(f"Action click target NOT VISIBLE: text={target_text} selector={target_selector}")
                    return

                self._dbg(f"Action click target_text={target_text} selector={target_selector}")
                
                # PREVENT NATIVE FILE PICKER STUCK
                try:
                    is_file_input = False
                    if await loc.count() > 0:
                        tag = await loc.evaluate("el => el.tagName.toLowerCase()")
                        attr_type = await loc.get_attribute("type")
                        if tag == "input" and attr_type == "file":
                            is_file_input = True

                    if is_file_input or (target_text and "attach" in target_text.lower()):
                        self._dbg("Click target seems to be a file upload. Redirecting to 'upload' logic.")
                        file_input = loc if is_file_input else loc.locator("xpath=..//input[@type='file'] | .//input[@type='file']").first
                        input_key = ""
                        if await file_input.count() > 0:
                            try:
                                input_id = await file_input.get_attribute("id") or ""
                                input_name = await file_input.get_attribute("name") or ""
                                input_label = await file_input.evaluate(
                                    "(el) => el.getAttribute('aria-label') || (el.labels && el.labels[0] ? el.labels[0].innerText : '') || ''"
                                )
                                input_key = f"{input_id} {input_name} {input_label}".lower()
                            except Exception:
                                input_key = ""

                        resume_path = getattr(self.profile, "resume_path", "")
                        cover_path = getattr(self.profile, "cover_letter_template_path", "") or getattr(self.profile, "cover_letter_path", "")
                        key = f"{input_key} {target_text or ''} {target_selector or ''}".lower()
                        if "cover" in key:
                            value = cover_path
                        elif "resume" in key or "cv" in key:
                            value = resume_path
                        else:
                            value = resume_path or cover_path

                        if not value or not os.path.exists(value):
                            self._dbg("Skipping upload: No file path available for this input.")
                            return
                        if await file_input.count() > 0:
                            await file_input.set_input_files(value)
                            return
                except Exception as e:
                    self._dbg(f"File redirect check failed (ignoring): {e}")

                # BLOCK SUBMISSION IN REVIEW MODE
                submit_keywords = ["submit", "send", "finish", "complete", "apply for this job"]
                if getattr(self.config, "mode", "auto_submit") == "review":
                    if target_text and any(k in target_text.lower() for k in submit_keywords):
                        self._dbg(f"REVIEW MODE: Blocking submission click on '{target_text}'")
                        return
                    if target_selector and any(k in target_selector.lower() for k in submit_keywords):
                        self._dbg(f"REVIEW MODE: Blocking submission click on selector '{target_selector}'")
                        return

                await loc.click()
                
                # If it was a submit button, check for validation errors and wait
                submit_terms = ["submit", "send", "apply", "finish", "complete"]
                submit_hit = any(k in (target_text or "").lower() for k in submit_terms)
                submit_hit = submit_hit or any(k in (target_selector or "").lower() for k in submit_terms)
                if submit_hit:
                    self._register_submit_attempt()
                    self._dbg("Submit clicked. Checking for validation errors...")
                    await page.wait_for_timeout(1000)
                    
                    # Check for validation errors
                    error_selectors = [
                        ".error:visible",
                        "[class*='error']:visible",
                        "[role='alert']:visible",
                        ".field-error:visible",
                        "[aria-invalid='true']"
                    ]
                    
                    has_errors = False
                    for sel in error_selectors:
                        try:
                            if await page.locator(sel).count() > 0:
                                error_text = await page.locator(sel).first.text_content()
                                self._dbg(f"VALIDATION ERROR DETECTED: {error_text}")
                                has_errors = True
                                break
                        except:
                            pass
                    
                    if not has_errors:
                        self._dbg("No validation errors. Waiting 5s for page transition/OTP...")
                        await page.wait_for_timeout(5000)
                    else:
                        self._dbg("Validation errors present. Form not submitted.")
                        self._clear_submit_pending("validation errors")
                    
                return
            elif action == "fill":
                loc = await self._find_locator(page, target_selector, field_label or target_text, role_button=False)
                if loc:
                    loc = loc.filter(visible=True).first
                    if await loc.count() > 0:
                        if await self._handle_country_select(page, loc, field_label or target_text, value):
                            return
                        self._dbg(f"Action fill field={field_label or target} value={value}")
                        await loc.fill(str(value or ""))
                    return
            elif action == "select":
                loc = await self._find_locator(page, target_selector, field_label or target_text, role_button=False)
                if loc:
                    loc = loc.filter(visible=True).first
                    if await loc.count() == 0:
                        return

                    option_to_select = str(option_label or value or "")

                    if self._is_placeholder_value(option_to_select):
                        fallback = self._fallback_select_value(field_label or target_text)
                        if fallback:
                            option_to_select = fallback

                    # Special Greenhouse Mapping: If question ID is used, it's often a select
                    if target and "question_" in target:
                         # Ensure we use mapped Yes/No if provided
                         if option_to_select.lower() in ("yes", "no"):
                              option_to_select = option_to_select.capitalize()

                    if await self._handle_country_select(page, loc, field_label or target_text, option_to_select):
                        return

                    self._dbg(f"Action select field={field_label or target} option={option_to_select}")
                    try:
                        # 1. Try standard HTML select
                        if self._is_placeholder_value(option_to_select):
                            try:
                                tag = await loc.evaluate("el => el.tagName.toLowerCase()")
                            except Exception:
                                tag = ""
                            if tag == "select":
                                try:
                                    options = await loc.locator("option").all_text_contents()
                                    for opt in options:
                                        if opt and opt.strip() and "select" not in opt.lower():
                                            option_to_select = opt.strip()
                                            break
                                except Exception:
                                    pass
                        await loc.select_option(label=option_to_select, timeout=2000)
                    except Exception:
                        # 2. Handle Custom Dropdowns (Greenhouse, React-Select, etc.)
                        try:
                            # Click the dropdown to open it
                            self._dbg(f"Opening custom dropdown for {field_label}")
                            await loc.click(timeout=3000)
                            await page.wait_for_timeout(500)
                            
                            # Find matching option in the dropdown list WITHOUT typing
                            # Greenhouse uses role="option" or data-value or simple text
                            candidates = [
                                page.locator(f'[role="option"]:has-text("{option_to_select}")').filter(visible=True),
                                page.locator(f'.select__option:has-text("{option_to_select}")').filter(visible=True),
                                page.locator(f'li[role="option"]:has-text("{option_to_select}")').filter(visible=True),
                                page.locator(f'li:has-text("{option_to_select}")').filter(visible=True),
                            ]
                            if option_to_select.lower() not in ("select...", "select", "--select--"):
                                try:
                                    candidates.append(
                                        page.get_by_text(option_to_select, exact=False)
                                        .filter(visible=True)
                                        .filter(has_not=page.locator(".select__single-value, .select__placeholder"))
                                    )
                                except Exception:
                                    candidates.append(page.get_by_text(option_to_select, exact=False).filter(visible=True))
                            
                            clicked = False
                            for cand in candidates:
                                if await cand.count() > 0:
                                    self._dbg(f"Found visible option match for '{option_to_select}', clicking...")
                                    await cand.first.click(timeout=3000)
                                    clicked = True
                                    break

                            if not clicked:
                                # Fallback: type and press Enter only if clicking failed
                                self._dbg(f"Could not find clickable option, trying keyboard navigation...")
                                if self._is_placeholder_value(option_to_select):
                                    await page.keyboard.press("ArrowDown")
                                else:
                                    await page.keyboard.type(option_to_select)
                                await page.wait_for_timeout(500)
                                await page.keyboard.press("Enter")
                        except Exception as e2:
                            self._dbg(f"Custom select failed: {e2}. Falling back to fill.")
                            await loc.fill(option_to_select)
                    return
            elif action == "check":
                loc = await self._find_locator(page, target_selector, field_label or target_text, role_button=False)
                if loc:
                    try:
                        await loc.check()
                    except Exception:
                        await loc.click()
                    self._dbg(f"Action check field={field_label or target}")
                    return
            elif action == "upload":
                loc = await self._find_locator(page, target_selector, field_label or target_text, role_button=False)
                if loc:
                    loc = loc.filter(visible=True).first
                    if await loc.count() > 0:
                        # Ensure it's an input[type=file]
                        tag = await loc.evaluate("el => el.tagName.toLowerCase()")
                        attr_type = await loc.get_attribute("type")
                        if tag != "input" or attr_type != "file":
                             # Find input[type=file] inside or nearby
                             sub = loc.locator("input[type='file']").first
                             if await sub.count() > 0:
                                 loc = sub
                             else:
                                 # Greenhouse often has the label but hidden input
                                 # We try to find any file input on page if target fails
                                 self._dbg("Target is not input[file], searching page for any file input...")
                                 loc = page.locator("input[type='file']").first

                        if await loc.count() > 0:
                            # Final check
                            final_attr_type = await loc.get_attribute("type")
                            if final_attr_type == "file":
                                if value:
                                    self._dbg(f"Action upload field={field_label or target} file={value}")
                                    await loc.set_input_files(str(value))
                                else:
                                    self._dbg(f"Skipping upload: Empty file path for {field_label or target}")
                            else:
                                self._dbg(f"Aborting upload: Target {target} is not a file input (type={final_attr_type})")
                    return
        except Exception as e:
            self._dbg(f"Action execution error: {e}")
            return

    def _infer_country_from_profile(self) -> Optional[str]:
        raw_country = getattr(self.profile, "country", None)
        if raw_country is not None:
            country = str(raw_country).strip()
            if country:
                return country

        location = (getattr(self.profile, "location", "") or "").strip()
        loc_norm = re.sub(r"[^a-z]+", " ", location.lower()).strip()
        if loc_norm:
            loc_tokens = f" {loc_norm} "
            if " united states " in loc_tokens or " usa " in loc_tokens or " u s " in loc_tokens:
                return "United States"
            if " united kingdom " in loc_tokens or " uk " in loc_tokens or " u k " in loc_tokens or " england " in loc_tokens or " scotland " in loc_tokens or " wales " in loc_tokens:
                return "United Kingdom"
            if "ireland" in loc_norm:
                return "Ireland"
            if "canada" in loc_norm:
                return "Canada"
            if "india" in loc_norm:
                return "India"
            if "australia" in loc_norm:
                return "Australia"
            if "new zealand" in loc_norm:
                return "New Zealand"
            if "singapore" in loc_norm:
                return "Singapore"
            if "germany" in loc_norm:
                return "Germany"
            if "france" in loc_norm:
                return "France"
            if "spain" in loc_norm:
                return "Spain"
            if "italy" in loc_norm:
                return "Italy"
            if "netherlands" in loc_norm:
                return "Netherlands"
            if "sweden" in loc_norm:
                return "Sweden"
            if "norway" in loc_norm:
                return "Norway"
            if "switzerland" in loc_norm:
                return "Switzerland"
            if "poland" in loc_norm:
                return "Poland"
            if "portugal" in loc_norm:
                return "Portugal"
            if "mexico" in loc_norm:
                return "Mexico"
            if "brazil" in loc_norm:
                return "Brazil"
            if "china" in loc_norm:
                return "China"
            if "japan" in loc_norm:
                return "Japan"
            if "south korea" in loc_norm or "korea" in loc_norm:
                return "South Korea"
            if "united arab emirates" in loc_norm or " uae " in loc_tokens:
                return "United Arab Emirates"

            match = re.search(r",\\s*([A-Za-z]{2})\\b", location)
            if match:
                state = match.group(1).upper()
                us_states = {
                    "AL", "AK", "AZ", "AR", "CA", "CO", "CT", "DE", "FL", "GA",
                    "HI", "ID", "IL", "IN", "IA", "KS", "KY", "LA", "ME", "MD",
                    "MA", "MI", "MN", "MS", "MO", "MT", "NE", "NV", "NH", "NJ",
                    "NM", "NY", "NC", "ND", "OH", "OK", "OR", "PA", "RI", "SC",
                    "SD", "TN", "TX", "UT", "VT", "VA", "WA", "WV", "WI", "WY",
                    "DC",
                }
                if state in us_states:
                    return "United States"

        phone = getattr(self.profile, "phone", "") or ""
        phone_clean = re.sub(r"[^0-9+]", "", phone)
        if phone_clean.startswith("+"):
            prefix_map = {
                "+1": "United States",
                "+91": "India",
                "+44": "United Kingdom",
                "+61": "Australia",
                "+64": "New Zealand",
                "+49": "Germany",
                "+33": "France",
                "+34": "Spain",
                "+39": "Italy",
                "+31": "Netherlands",
                "+46": "Sweden",
                "+47": "Norway",
                "+41": "Switzerland",
                "+48": "Poland",
                "+351": "Portugal",
                "+353": "Ireland",
                "+65": "Singapore",
                "+60": "Malaysia",
                "+62": "Indonesia",
                "+63": "Philippines",
                "+81": "Japan",
                "+82": "South Korea",
                "+86": "China",
                "+52": "Mexico",
                "+55": "Brazil",
                "+971": "United Arab Emirates",
            }
            for prefix in sorted(prefix_map.keys(), key=len, reverse=True):
                if phone_clean.startswith(prefix):
                    return prefix_map[prefix]

        return None

    def _map_token(self, val: Any) -> Any:
        if not isinstance(val, str):
            return val
        full_name = getattr(self.profile, "full_name", "")
        parts = full_name.split()
        first_name = parts[0] if parts else full_name
        last_name = parts[-1] if len(parts) > 1 else ""
        country = self._infer_country_from_profile() or ""
        tokens = {
            "$PROFILE.first_name": first_name,
            "$PROFILE.last_name": last_name,
            "$PROFILE.preferred_name": first_name,
            "$PROFILE.preferred_first_name": first_name,
            "$PROFILE.email": getattr(self.profile, "email", ""),
            "$PROFILE.full_name": getattr(self.profile, "full_name", ""),
            "$PROFILE.phone": getattr(self.profile, "phone", ""),
            "$PROFILE.location": getattr(self.profile, "location", ""),
            "$PROFILE.country": country,
            "$PROFILE.country_selected": country,
            "$PROFILE.linkedin": getattr(self.profile, "linkedin_url", ""),
            "$PROFILE.linkedin_url": getattr(self.profile, "linkedin_url", ""),
            "$PROFILE.linkedin_profile": getattr(self.profile, "linkedin_url", ""),
            "$PROFILE.github": getattr(self.profile, "github_url", ""),
            "$PROFILE.portfolio": getattr(self.profile, "portfolio_url", ""),
            "$PROFILE.work_auth": "Yes" if getattr(self.profile, "work_auth", False) else "No",
            "$PROFILE.sponsorship": "Yes" if getattr(self.profile, "sponsorship_needed", False) else "No",
            "$PROFILE.workday_password": getattr(self.profile, "workday_password", ""),
            "$PROFILE.resume_path": getattr(self.profile, "resume_path", ""),
            "$PROFILE.cover_letter": getattr(self.profile, "cover_letter_template_path", "") or getattr(self.profile, "cover_letter_path", ""),
            "$PROFILE.salary": "Competitive Market Rate",
            "$PROFILE.total_compensation": "Competitive Market Rate",
            "$PROFILE.experience": str(getattr(self.profile, "total_years_experience", "5")) + " years",
        }
        return tokens.get(val, val)

    def _normalize_planner_actions(self, actions: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        normalized = []
        for act in actions:
            if not isinstance(act, dict):
                continue
            action = (act.get("action") or "").lower()
            target_text = act.get("target_text") or act.get("field_label") or act.get("label") or act.get("target") or act.get("field")

            if action == "fill":
                raw_val = act.get("value")
                mapped = self._map_token(raw_val)
                if self._is_empty_or_unknown_token(mapped):
                    fallback = self._fallback_fill_value(target_text)
                    if fallback:
                        mapped = fallback
                act["value"] = mapped
            elif action == "select":
                raw_opt = act.get("option_label") or act.get("value")
                mapped = self._map_token(raw_opt)
                if self._is_placeholder_value(mapped):
                    fallback = self._fallback_select_value(target_text)
                    if fallback:
                        mapped = fallback
                if (not target_text or str(target_text).strip().lower() in ("select...", "select", "--select--")) and not act.get("target_selector"):
                    act["target_selector"] = "select:not([data-is-filled='true']), [role='combobox']:not([data-is-filled='true']), input[role='combobox']:not([data-is-filled='true'])"
                act["option_label"] = mapped
                act["value"] = mapped

            normalized.append(act)
        return normalized

    async def _filter_optional_file_actions(self, page, actions: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        filtered: List[Dict[str, Any]] = []
        cover_path = getattr(self.profile, "cover_letter_template_path", "") or getattr(self.profile, "cover_letter_path", "")
        resume_path = getattr(self.profile, "resume_path", "")
        for act in actions:
            action = (act.get("action") or "").lower()
            if action not in ("click", "upload"):
                filtered.append(act)
                continue

            target_text = act.get("target_text") or act.get("field_label") or act.get("label") or act.get("target") or act.get("field")
            target_selector = act.get("target_selector")
            key = f"{target_text or ''} {act.get('target') or ''} {act.get('field_label') or ''}".lower()
            if not any(k in key for k in ("attach", "upload", "resume", "cv", "cover")):
                filtered.append(act)
                continue

            loc = await self._find_locator(page, target_selector, target_text, role_button=True)
            if not loc:
                filtered.append(act)
                continue

            try:
                info = await loc.evaluate(
                    """(el) => {
                        const isFile = el.tagName && el.tagName.toLowerCase() === 'input' && el.type === 'file';
                        const findInput = (node) => {
                            if (!node) return null;
                            if (node.tagName && node.tagName.toLowerCase() === 'input' && node.type === 'file') return node;
                            const group = node.closest('.file-upload, [role="group"], .field');
                            if (group) {
                                const inp = group.querySelector('input[type="file"]');
                                if (inp) return inp;
                            }
                            const parent = node.parentElement;
                            if (parent) {
                                const inp = parent.querySelector('input[type="file"]');
                                if (inp) return inp;
                            }
                            return null;
                        };
                        const input = isFile ? el : findInput(el);
                        if (!input) return {found:false};
                        const group = input.closest('[role="group"]');
                        const required = !!input.required || input.getAttribute('aria-required') === 'true' || (group && group.getAttribute('aria-required') === 'true');
                        const key = ((input.id || '') + ' ' + (input.name || '') + ' ' + (input.getAttribute('aria-label') || '') + ' ' + (group ? (group.textContent || '') : '')).toLowerCase();
                        return {found:true, required: required, key: key};
                    }"""
                )
                if info and info.get("found") and not info.get("required"):
                    info_key = info.get("key") or ""
                    if ("cover" in info_key) and not cover_path:
                        self._dbg("Skipping optional cover letter upload action (no file).")
                        continue
                    if (("resume" in info_key) or ("cv" in info_key)) and not resume_path:
                        self._dbg("Skipping optional resume upload action (no file).")
                        continue
            except Exception:
                pass

            filtered.append(act)

        return filtered

    def _is_empty_or_unknown_token(self, val: Any) -> bool:
        if val is None:
            return True
        if isinstance(val, str):
            if val.strip() == "":
                return True
            if val.strip().startswith("$PROFILE."):
                return True
        return False

    def _is_placeholder_value(self, val: Any) -> bool:
        if val is None:
            return True
        if isinstance(val, str):
            if val.strip() == "":
                return True
            if val.strip().startswith("$PROFILE."):
                return True
            if val.strip().lower() in ("select...", "select", "--select--"):
                return True
        return False

    def _fallback_fill_value(self, target_text: Optional[str]) -> Optional[str]:
        text = (target_text or "").lower()
        if not text:
            return "See resume for details."
        if "salary" in text or "compensation" in text or "pay" in text:
            return "Competitive Market Rate"
        if "country" in text or "citizenship" in text or "nationality" in text:
            return self._infer_country_from_profile()
        if "linkedin" in text:
            return getattr(self.profile, "linkedin_url", "") or "https://www.linkedin.com"
        if "experience" in text or "background" in text or "describe" in text:
            years = getattr(self.profile, "total_years_experience", None)
            if years:
                return f"I have {years} years of experience relevant to this role. See resume for details."
            return "I have relevant experience for this role; see resume for details."
        if "why" in text or "interest" in text or "motivat" in text:
            return "I am excited about this role and believe my background aligns with the team's goals."
        return "See resume for details."

    def _fallback_select_value(self, target_text: Optional[str]) -> Optional[str]:
        text = (target_text or "").lower()
        if not text:
            return None
        if "country" in text or "citizenship" in text or "nationality" in text:
            return self._infer_country_from_profile()
        if "eligible" in text or "authorized" in text or "legally" in text:
            return "Yes" if getattr(self.profile, "work_auth", False) else "No"
        if "visa" in text or "sponsor" in text or "sponsorship" in text:
            return "Yes" if getattr(self.profile, "sponsorship_needed", False) else "No"
        if "remote" in text or "hybrid" in text or "onsite" in text or "location" in text:
            pref = getattr(self.profile, "preferred_location_type", "")
            if pref:
                return pref
        if "acknowledge" in text or "agree" in text or "consent" in text or "confirm" in text:
            return "Yes"
        return None

    async def _attempt_resume_prepass(self, page) -> bool:
        if self._resume_prepass_done:
            return True

        resume_path = getattr(self.profile, "resume_path", "")
        if not resume_path or not os.path.exists(resume_path):
            self._dbg("Resume prepass: resume file missing; skipping.")
            self._resume_prepass_done = True
            return False

        try:
            contexts = [page] + [f for f in page.frames if f != page.main_frame and not f.is_detached()]
        except Exception:
            contexts = [page]

        candidates: List[Dict[str, Any]] = []
        for ctx in contexts:
            try:
                loc = ctx.locator("input[type='file']")
                count = await loc.count()
            except Exception:
                continue
            if count <= 0:
                continue
            for i in range(count):
                inp = loc.nth(i)
                try:
                    info = await inp.evaluate(
                        """(el) => {
                            const id = el.id || "";
                            const name = el.name || "";
                            const aria = el.getAttribute("aria-label") || "";
                            const label = el.labels && el.labels[0] ? el.labels[0].innerText : "";
                            const group = el.closest('[role="group"], .file-upload, .field');
                            const groupText = group ? (group.innerText || "") : "";
                            const required = !!el.required || el.getAttribute('aria-required') === 'true' || (group && group.getAttribute('aria-required') === 'true');
                            const hasFile = el.files && el.files.length > 0;
                            return {id, name, aria, label, groupText, required, hasFile};
                        }"""
                    )
                except Exception:
                    continue

                if info and info.get("hasFile"):
                    self._dbg("Resume prepass: file already present.")
                    self._resume_prepass_done = True
                    return True

                key = f"{info.get('id','')} {info.get('name','')} {info.get('aria','')} {info.get('label','')} {info.get('groupText','')}".lower()
                candidates.append({"input": inp, "key": key, "required": bool(info.get("required"))})

        if not candidates:
            return False

        target = None
        for cand in candidates:
            if "resume" in cand["key"] or "cv" in cand["key"]:
                target = cand
                break
        if not target:
            for cand in candidates:
                if cand["required"] and "cover" not in cand["key"]:
                    target = cand
                    break
        if not target and len(candidates) == 1:
            target = candidates[0]

        if not target:
            self._dbg("Resume prepass: no suitable file input found yet.")
            return False

        try:
            await target["input"].set_input_files(resume_path)
            self._dbg("Resume prepass: uploaded resume.")
            self._resume_prepass_done = True
            return True
        except Exception as e:
            self._dbg(f"Resume prepass: upload failed: {e}")
            return False

    async def _handle_country_select(self, page, loc, target_text: Optional[str], option_to_select: Optional[str]) -> bool:
        text = (target_text or "").lower()
        if "country" not in text and "citizenship" not in text and "nationality" not in text:
            return False

        if option_to_select is None:
            desired = ""
        else:
            desired = option_to_select if isinstance(option_to_select, str) else str(option_to_select)
        inferred = self._infer_country_from_profile()
        if self._is_placeholder_value(desired) and inferred:
            desired = inferred

        if not desired:
            return False

        self._dbg(f"Country select helper: target={target_text} option={desired}")

        try:
            tag = await loc.evaluate("el => el.tagName.toLowerCase()")
        except Exception:
            tag = ""

        if tag == "select":
            try:
                await loc.select_option(label=desired, timeout=2000)
                return True
            except Exception:
                pass

        try:
            await loc.click(timeout=3000)
            await page.wait_for_timeout(200)
        except Exception:
            pass

        try:
            iti = loc.locator("xpath=ancestor-or-self::*[contains(@class,'iti')]").first
            if await iti.count() > 0:
                btn = iti.locator(".iti__selected-country, .iti__selected-flag").first
                if await btn.count() > 0:
                    await btn.click(timeout=2000)
                    await page.wait_for_timeout(200)
                country_list = iti.locator(".iti__country-list, [role='listbox']")
                opt = country_list.locator(".iti__country, [role='option']").filter(has_text=desired).first
                if await opt.count() > 0:
                    await opt.click(timeout=3000)
                    return True
        except Exception:
            pass

        try:
            listbox = page.locator("[role='listbox']")
            opt = listbox.locator("[role='option'], .select__option, li").filter(has_text=desired).first
            if await opt.count() > 0:
                await opt.click(timeout=3000)
                return True
        except Exception:
            pass

        try:
            opt = page.locator(".select__option").filter(has_text=desired).first
            if await opt.count() > 0:
                await opt.click(timeout=3000)
                return True
        except Exception:
            pass

        try:
            await loc.fill("")
            await loc.type(desired)
            await page.wait_for_timeout(200)
            await page.keyboard.press("Enter")
            return True
        except Exception:
            return False

    async def _find_locator(self, page, target_selector: Optional[str], target_text: Optional[str], role_button: bool = False):
        candidates = []
        if target_selector:
            candidates.append(page.locator(target_selector))
        if target_text:
            if role_button:
                candidates.append(page.get_by_role("button", name=target_text))
                candidates.append(page.get_by_role("link", name=target_text)) # Add links
                candidates.append(page.locator(f"button:has-text('{target_text}')"))
                candidates.append(page.locator(f"a:has-text('{target_text}')")) # Add links
                candidates.append(page.locator(f"input[type='submit'][value*='{target_text}' i]"))
                candidates.append(page.locator(f"input[type='button'][value*='{target_text}' i]"))
            else:
                candidates.append(page.get_by_label(target_text, exact=False))
                candidates.append(page.get_by_text(target_text, exact=False))
                candidates.append(page.get_by_placeholder(target_text, exact=False))

        # Search main page
        for loc in candidates:
            try:
                if await loc.count() > 0:
                    refined = await self._refine_locator(loc.first, role_button)
                    if refined: return refined
            except Exception:
                continue

        # Search iframes
        for fr in page.frames:
            if fr == page.main_frame: continue
            for seed in candidates: # This is a bit tricky since seeds are bound to page
                # Re-create seeds for frame
                frame_candidates = []
                if target_selector: frame_candidates.append(fr.locator(target_selector))
                if target_text:
                    if role_button:
                        frame_candidates.append(fr.get_by_role("button", name=target_text))
                        frame_candidates.append(fr.get_by_role("link", name=target_text))
                        frame_candidates.append(fr.locator(f"button:has-text('{target_text}')"))
                        frame_candidates.append(fr.locator(f"a:has-text('{target_text}')"))
                    else:
                        frame_candidates.append(fr.get_by_label(target_text, exact=False))
                        frame_candidates.append(fr.get_by_text(target_text, exact=False))
                
                for f_loc in frame_candidates:
                    try:
                        if await f_loc.count() > 0:
                            refined = await self._refine_locator(f_loc.first, role_button)
                            if refined: return refined
                    except Exception:
                        continue
        return None

    async def _refine_locator(self, loc, is_button: bool):
        """
        If we found a label or text, find the actual input/button nearby.
        """
        try:
            tag = await loc.evaluate("el => el.tagName.toLowerCase()")
            if is_button:
                if tag in ("button", "input"): return loc
                # search for button inside or parent
                btn = loc.locator("button, input[type='button'], input[type='submit']").first
                if await btn.count() > 0: return btn
                return loc # Fallback
            else:
                if tag in ("input", "textarea", "select"): return loc
                # if it's a label, for attribute?
                for_id = await loc.get_attribute("for")
                if for_id:
                    # Search globally or in parent container
                    input_loc = loc.page.locator(f"#{for_id}").first
                    if await input_loc.count() > 0: return input_loc
                
                # Search for input/select/textarea inside or immediately after
                # Greenhouse often puts label and input in same div
                # Try to go up to container and find input
                container = loc.locator("xpath=..")
                sub_input = container.locator("input, textarea, select").first
                if await sub_input.count() > 0: return sub_input
                
                # Check siblings
                next_input = loc.locator("xpath=./following-sibling::input | ./following-sibling::div//input").first
                if await next_input.count() > 0: return next_input
            
            return loc
        except Exception:
            return loc

    async def _is_workday_account_creation(self, page) -> bool:
        try:
            html = (await page.content()).lower()
        except Exception:
            html = ""
        markers = [
            "create account/sign in",
            "create account",
            "thank you for creating your candidate home account",
        ]
        if any(m in html for m in markers):
            # also check for password inputs to confirm
            try:
                pwd_inputs = await page.locator("input[type='password']").count()
                if pwd_inputs > 0:
                    return True
            except Exception:
                pass
        return False

    async def _handle_workday_signin(self, page) -> bool:
        password = getattr(self.profile, "workday_password", None)
        if not password:
            return False
        try:
            # Click "Sign In" link if present
            try:
                signin_link = page.get_by_text("Sign In", exact=False)
                if await signin_link.count() > 0:
                    await signin_link.first.click()
                    await page.wait_for_timeout(500)
            except Exception:
                pass
            email_field = page.locator("input[type='email'], input[name*='email' i]")
            pwd_field = page.locator("input[type='password']").first
            if await email_field.count() > 0:
                await email_field.first.fill(self.profile.email)
            if await pwd_field.count() > 0:
                await pwd_field.fill(password)
            # click submit
            candidates = [
                page.get_by_role("button", name="Sign In"),
                page.locator("button:has-text('Sign In')"),
                page.locator("input[type='submit']"),
                page.locator("button[data-automation-id='signInSubmitButton']"),
            ]
            for btn in candidates:
                try:
                    if await btn.count() > 0 and await btn.first.is_enabled():
                        await btn.first.click()
                        await page.wait_for_timeout(1500)
                        return True
                except Exception:
                    continue
        except Exception:
            return False
        return False

    async def _handle_workday_account_creation(self, page) -> bool:
        password = getattr(self.profile, "workday_password", None)
        if not password:
            return False
        try:
            email_field = page.locator("input[type='email'], input[name*='email' i]")
            pwd_field = page.locator("input[type='password']").nth(0)
            pwd_verify = page.locator("input[type='password']").nth(1)
            if await email_field.count() > 0:
                await email_field.first.fill(self.profile.email)
            if await pwd_field.count() > 0:
                await pwd_field.fill(password)
            if await pwd_verify.count() > 0:
                await pwd_verify.fill(password)
            # accept data privacy if present
            try:
                checkbox = page.locator("input[type='checkbox']")
                if await checkbox.count() > 0:
                    await checkbox.first.check()
            except Exception:
                pass
            # click create account
            buttons = [
                "button:has-text('Create Account')",
                "button[data-automation-id='createAccountSubmitButton']",
            ]
            for sel in buttons:
                try:
                    btn = page.locator(sel)
                    if await btn.count() > 0 and await btn.first.is_enabled():
                        await btn.first.click()
                        await page.wait_for_timeout(2000)
                        return True
                except Exception:
                    continue
        except Exception:
            return False
        return False

    async def _gemini_extract(self, page):
        try:
            html = await self._get_page_content_with_frames(page)
        except Exception:
            return []
        if self._debug_enabled:
            try:
                snap = (html or "")[:2000]
                print("[DEBUG] Analyzer HTML snapshot (first 2000 chars):")
                print(snap)
            except Exception:
                pass
        data = analyze_html(html, getattr(self.config, "gemini_api_key", ""), getattr(self.config, "gemini_model", "models/gemini-2.5-flash"))
        qbs = []
        if not data or "fields" not in data:
            return qbs
        for f in data.get("fields", []):
            try:
                label = f.get("label") or ""
                ftype = (f.get("type") or "text").lower()
                opts = f.get("options") or None
                qbs.append(
                    QuestionBlock(
                        locator=f"//label[contains(.,\"{label}\")]/following::input[1]",
                        field_type="text" if ftype in ("password", "email") else ftype,
                        question_text=label,
                        options=[Option(label=o, locator="") for o in opts] if opts else None,
                        multiple=(ftype == "checkbox"),
                    )
                )
            except Exception:
                continue
        return qbs

    async def _capture_debug(self, page, url: str, reason: str):
        ts = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
        base = self.debug_dir / f"debug-{ts}"
        try:
            await page.screenshot(path=str(base.with_suffix(".png")))
        except Exception:
            pass
        try:
            html = await page.content()
            base.with_suffix(".html").write_text(html, encoding="utf-8")
        except Exception:
            pass
        if self.logger:
            self.logger.log(
                {
                    "debug": True,
                    "url": url,
                    "reason": reason,
                    "ts": datetime.now(timezone.utc).isoformat(),
                    "iframe_count": await page.locator("iframe").count(),
                }
            )

    def _dbg(self, msg: str) -> None:
        if self._debug_enabled:
            if self.logger:
                self.logger.log({"debug": True, "message": msg, "ts": datetime.now(timezone.utc).isoformat()})
            else:
                try:
                    print(f"[DEBUG] {msg}")
                except Exception:
                    pass

    def _register_submit_attempt(self) -> None:
        self._submit_pending = True
        self._submit_started_at = time.monotonic()
        self._submit_started_utc = datetime.now(timezone.utc)

    def _clear_submit_pending(self, reason: str) -> None:
        if self._submit_pending:
            self._dbg(f"Clearing submit pending: {reason}")
        self._submit_pending = False
        self._submit_started_at = None
        self._submit_started_utc = None

    def _submit_wait_exceeded(self) -> bool:
        if not self._submit_pending or self._submit_started_at is None:
            return False
        return (time.monotonic() - self._submit_started_at) > self._submit_timeout_sec

    async def _has_validation_errors(self, page) -> bool:
        selectors = [
            ".error",
            "[class*='error']",
            "[role='alert']",
            ".field-error",
            ".helper-text--error",
            "[aria-invalid='true']",
        ]
        try:
            contexts = [page] + [f for f in page.frames if f != page.main_frame and not f.is_detached()]
        except Exception:
            contexts = [page]
        for ctx in contexts:
            for sel in selectors:
                try:
                    loc = ctx.locator(sel).filter(visible=True)
                    if await loc.count() > 0:
                        return True
                except Exception:
                    continue
        return False

    async def _get_page_content_with_frames(self, page) -> str:
        """
        Aggregate content from main page and all accessible iframes.
        """
        full_name = getattr(self.profile, "full_name", "")
        parts = full_name.split()
        first_name = parts[0] if parts else ""
        last_name = parts[-1] if len(parts) > 1 else ""
        country = self._infer_country_from_profile() or ""
        
        profile_data = {
            "email": getattr(self.profile, "email", ""),
            "phone": getattr(self.profile, "phone", ""),
            "first_name": first_name,
            "last_name": last_name,
            "full_name": full_name,
            "country": country,
            "linkedin": getattr(self.profile, "linkedin_url", ""),
            "resume_path": getattr(self.profile, "resume_path", ""),
            "cover_letter": getattr(self.profile, "cover_letter_template_path", "") or getattr(self.profile, "cover_letter_path", ""),
            "salary": "Competitive Market Rate", # Match _map_token
            "experience": str(getattr(self.profile, "total_years_experience", "5")) + " years",
            "work_auth": "Yes" if getattr(self.profile, "work_auth", True) else "No",
            "sponsorship": "Yes" if getattr(self.profile, "sponsorship_needed", False) else "No",
        }

        async def inject_values(target):
            try:
                # We pass profile_data into the expression
                await target.evaluate("""(profile) => {
                    const inputs = document.querySelectorAll('input, textarea, select');
                    
                    const reverseMap = {};
                    const add = (val, tok) => { if(val) reverseMap[String(val).toLowerCase().trim()] = tok; };
                    
                    add(profile.email, '$PROFILE.email');
                    if (profile.phone) {
                         const p = profile.phone.replace(/[^0-9+]/g, '');
                         if(p) reverseMap[p] = '$PROFILE.phone';
                    }
                    add(profile.first_name, '$PROFILE.first_name');
                    add(profile.last_name, '$PROFILE.last_name');
                    add(profile.full_name, '$PROFILE.full_name');
                    add(profile.country, '$PROFILE.country');
                    add(profile.linkedin, '$PROFILE.linkedin');
                    add(profile.salary, '$PROFILE.salary');
                    add(profile.experience, '$PROFILE.experience');
                    add(profile.work_auth, '$PROFILE.work_auth');
                    add(profile.sponsorship, '$PROFILE.sponsorship');
                    
                    // Safety for Yes/No common patterns
                    add("Yes", "Yes");
                    add("No", "No");

                    for (const el of inputs) {
                        const tag = el.tagName.toLowerCase();
                        let isFilled = false;
                        if (el.type === 'checkbox' || el.type === 'radio') {
                            if (el.checked) {
                                el.setAttribute('checked', 'checked');
                                isFilled = true;
                            } else el.removeAttribute('checked');
                        } else if (tag === 'select') {
                            const opt = el.options[el.selectedIndex];
                            if (opt && opt.value && opt.value !== "" && !opt.text.toLowerCase().includes('select')) {
                                el.setAttribute('data-selected-text', opt.text);
                                isFilled = true;
                                const tok = reverseMap[opt.text.toLowerCase().trim()];
                                if(tok) el.setAttribute('data-matches-profile', tok);
                            }
                        } else {
                            const val = el.value || '';
                            el.setAttribute('value', val);
                            if (val.trim() !== "") isFilled = true;
                            
                            const cleanVal = val.toLowerCase().trim();
                            const phoneClean = val.replace(/[^0-9+]/g, '');
                            
                            if (reverseMap[cleanVal]) {
                                el.setAttribute('data-matches-profile', reverseMap[cleanVal]);
                            } else if (phoneClean && reverseMap[phoneClean]) {
                                el.setAttribute('data-matches-profile', reverseMap[phoneClean]);
                            }
                        }
                        // File detection
                        if (el.type === 'file' && (el.files && el.files.length > 0)) {
                             isFilled = true;
                        }
                        // Treat optional file uploads as filled when no file is available
                        if (el.type === 'file' && !isFilled) {
                            const group = el.closest('[role="group"], .file-upload');
                            const required = el.required || el.getAttribute('aria-required') === 'true' || (group && group.getAttribute('aria-required') === 'true');
                            if (!required) {
                                const key = ((el.id || '') + ' ' + (el.name || '')).toLowerCase();
                                const isCover = key.includes('cover');
                                const isResume = key.includes('resume') || key.includes('cv');
                                const hasCover = profile.cover_letter && profile.cover_letter.length > 0;
                                const hasResume = profile.resume_path && profile.resume_path.length > 0;
                                if ((isCover && !hasCover) || (isResume && !hasResume)) {
                                    isFilled = true;
                                    el.setAttribute('data-is-filled', 'true');
                                }
                            }
                        }
                        // Greenhouse hidden file detection (check for filenames/remove buttons nearby)
                        const container = el.closest('.field, .select-shell, [class*="upload"]');
                        if (container && (container.innerText.includes('.pdf') || container.innerText.includes('.doc') || container.querySelector('button[aria-label*="Remove"]'))) {
                             isFilled = true;
                        }

                        if (isFilled) el.setAttribute('data-is-filled', 'true');
                    }
                    
                    // Special for Greenhouse/React-Select Components:
                    for (const valDiv of document.querySelectorAll('.select__single-value')) {
                        const txt = valDiv.innerText.trim();
                        const clean = txt.toLowerCase();
                        // Find associated input (usually a sibling or inside same shell)
                        const shell = valDiv.closest('.select-shell, .field, [class*="select-container"]');
                        if (shell) {
                            const input = shell.querySelector('input');
                            if (input) {
                                input.setAttribute('data-is-filled', 'true');
                                input.setAttribute('data-selected-text', txt);
                                if (reverseMap[clean]) {
                                    input.setAttribute('data-matches-profile', reverseMap[clean]);
                                }
                            }
                        }
                    }
                    // Special for Greenhouse/Modern Country Pickers & Custom Selects:
                    for (const b of document.querySelectorAll('button, a.chosen-single, div.select-button, .select__control')) {
                        const txt = b.innerText.trim();
                        const clean = txt.toLowerCase();
                        
                        // Generic "Chosen" or custom dropdowns
                        if (reverseMap[clean]) {
                           b.setAttribute('data-matches-profile', reverseMap[clean]);
                           b.setAttribute('data-is-filled', 'true');
                        }
                    }
                    // Extract and annotate dropdown options for Gemini
                    for (const select of document.querySelectorAll('select')) {
                        const options = Array.from(select.options)
                            .filter(opt => opt.value && opt.value !== "" && !opt.text.toLowerCase().includes('select'))
                            .map(opt => opt.text.trim());
                        if (options.length > 0) {
                            select.setAttribute('data-available-options', JSON.stringify(options));
                        }
                    }
                    
                    // For custom Greenhouse dropdowns (React-Select style)
                    for (const dropdown of document.querySelectorAll('[class*="select"], [role="combobox"]')) {
                        // Try to find the options menu (might be hidden until clicked)
                        const menu = dropdown.closest('.field')?.querySelector('[role="listbox"], [class*="menu"]');
                        if (menu) {
                            const options = Array.from(menu.querySelectorAll('[role="option"], li'))
                                .map(opt => opt.innerText.trim())
                                .filter(txt => txt && txt.length > 0 && txt.length < 100);
                            if (options.length > 0) {
                                dropdown.setAttribute('data-available-options', JSON.stringify(options));
                            }
                        }
                    }
                    
                    // Mark Submit Button State
                    for (const btn of document.querySelectorAll('button[type="submit"], input[type="submit"]')) {
                        const isDisabled = btn.disabled || btn.getAttribute('aria-disabled') === 'true' || btn.classList.contains('btn__disabled');
                        btn.setAttribute('data-is-disabled', isDisabled ? 'true' : 'false');
                    }
                }""", profile_data)
            except Exception:
                pass

        await inject_values(page)

        main_content = await page.content()
        
        # 2. Iterate frames
        frames_content = []
        try:
            frames = page.frames
            self._dbg(f"Iframe extraction: Found {len(frames)} frames")
            for i, f in enumerate(frames):
                try:
                    # Skip detached or main frame (already got main)
                    if f.is_detached() or f == page.main_frame:
                        continue
                    
                    # Inject values into frame too
                    await inject_values(f)
                    
                    title = await f.title()
                    url = f.url
                    c = await f.content()
                    if c:
                        self._dbg(f"  - Frame {i}: {title} ({url}) -> {len(c)} chars")
                        # Wrap in a simple delimiter so LLM knows it's a frame
                        frames_content.append(f"\n<!-- IFRAME START: {title} -->\n{c}\n<!-- IFRAME END -->\n")
                except Exception as e:
                    self._dbg(f"  - Frame {i} extraction failed: {e}")
                    pass
        except Exception:
            pass

        return main_content + "\n".join(frames_content)
