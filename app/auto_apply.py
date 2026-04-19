# app/auto_apply_headless.py
from __future__ import annotations

import sys, asyncio, time
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Any, Optional
from urllib.parse import urlparse
import json
from playwright.sync_api import sync_playwright, Error
import os
from pathlib import Path

# --- add near top ---
import re, requests

_ATS_PATTERNS = [
    ("lever",       re.compile(r"(?:jobs|apply)\.lever\.co", re.I)),
    ("greenhouse",  re.compile(r"(?:boards|app)\.greenhouse\.io", re.I)),
    ("ashby",       re.compile(r"jobs\.ashbyhq\.com", re.I)),
    ("workday",     re.compile(r"(?:myworkdayjobs|wd1\.myworkdayjobs)\.com", re.I)),
    ("smartrecruiters", re.compile(r"smartrecruiters\.com", re.I)),
    # add more as you implement handlers
]

def _resolve_final_url(url: str, timeout=8) -> str:
    try:
        # Enforce strict connect AND read timeouts to prevent SSL proxy hangs
        r = requests.get(url, allow_redirects=True, timeout=(timeout, timeout))
        return r.url or url
    except Exception:
        return url

def _weworkremotely_submit(page, url: str, profile: AutoApplyProfile) -> AutoApplyResult:
    """
    Handle WeWorkRemotely listings: find external apply link, follow it, 
    then delegate to ATS-specific submitter.
    """
    try:
        page.goto(url, wait_until="domcontentloaded")
        # Click or extract the "Apply for this position" button
        link = None
        try:
            loc = page.locator("a:has-text('Apply for this position')")
            if loc.count() > 0:
                link = loc.first.get_attribute("href")
                if not link.startswith("http"):
                    # relative link → make absolute
                    from urllib.parse import urljoin
                    link = urljoin(url, link)
        except Exception:
            pass

        if not link:
            return AutoApplyResult(False, "wwr-no-link", {"url": url})

        # Follow to external ATS
        final_url = _resolve_final_url(link)
        ats = _guess_ats_from_url(final_url)

        # Open in same page for simplicity
        page.goto(final_url, wait_until="domcontentloaded")

        if ats == "lever":
            res = _lever_submit(page, final_url, profile)
        elif ats == "greenhouse":
            res = _greenhouse_submit(page, final_url, profile)
        elif ats == "ashby":
            res = AutoApplyResult(False, "ashby-todo", {"url": final_url})
        elif ats == "workday":
            res = _workday_submit(page, final_url, profile)
        elif ats == "smartrecruiters":
            res = AutoApplyResult(False, "smartrecruiters-todo", {"url": final_url})
        else:
            res = AutoApplyResult(False, "opened-manual", {"url": final_url, "ats_type": ats or "unknown"})

        res.details["from"] = "weworkremotely"
        return res

    except Exception as e:
        return AutoApplyResult(False, "wwr-error", {"error": str(e), "url": url})


def _guess_ats_from_url(url: str) -> str:
    host = (urlparse(url).hostname or "").lower()
    # fast path for common ones
    if "lever.co" in host: return "lever"
    if "greenhouse.io" in host or "boards.greenhouse.io" in host: return "greenhouse"
    # generalized pattern search
    for name, pat in _ATS_PATTERNS:
        if pat.search(url):
            return name
    return ""

# Ensure a Windows-compatible event loop for subprocess launches (Playwright)
if sys.platform.startswith("win"):
    try:
        asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())
    except Exception:
        pass

# Optional YAML profile support
try:
    import yaml  # type: ignore
    _HAS_YAML = True
except Exception:
    _HAS_YAML = False

# Playwright (required)
try:
    from playwright.sync_api import sync_playwright
    _HAS_PLAYWRIGHT = True
except Exception:
    _HAS_PLAYWRIGHT = False


# ---------- Types ----------
@dataclass
class AutoApplyProfile:
    full_name: str
    email: str
    phone: str
    resume_path: str
    location: Optional[str] = None
    cover_letter_path: Optional[str] = None
    linkedin_url: Optional[str] = None
    github_url: Optional[str] = None
    portfolio_url: Optional[str] = None

    def validate(self) -> None:
        rp = Path(self.resume_path)
        if not rp.exists():
            raise FileNotFoundError(f"Resume not found: {rp}")
        if self.cover_letter_path:
            cp = Path(self.cover_letter_path)
            if not cp.exists():
                raise FileNotFoundError(f"Cover letter not found: {cp}")


@dataclass
class AutoApplyResult:
    ok: bool
    status: str
    details: Dict[str, Any]

    def to_meta_entry(self) -> Dict[str, Any]:
        return {
            "mode": "ats-headless",
            "ok": self.ok,
            "status": self.status,
            "details": self.details,
            "ts": datetime.now(timezone.utc).isoformat(),
        }

# Common launch args (helps with headless reliability)
_LAUNCH_ARGS = [
    "--disable-gpu",
    "--disable-dev-shm-usage",
    "--no-sandbox",
    "--disable-blink-features=AutomationControlled",
]

def _default_chrome_paths():
    paths = []
    # Windows common installs
    paths += [
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    ]
    # macOS
    paths += ["/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"]
    # Linux
    paths += ["/usr/bin/google-chrome", "/usr/bin/chromium", "/snap/bin/chromium"]
    return [p for p in paths if Path(p).exists()]

def _launch_browser(p, headless: bool = True, executable_path: str | None = None):
    """
    Try standard bundled Chromium first; if missing, fall back to system Chrome.
    You can also force the path via env AA_CHROME.
    """
    # 1) Try bundled chromium (requires `playwright install`)
    try:
        return p.chromium.launch(
            headless=headless, 
            args=_LAUNCH_ARGS + (["--headless=new"] if headless else [])
        )
    except Exception:
        pass

    # 2) Try system Chrome via channel
    try:
        return p.chromium.launch(
            channel="chrome", 
            headless=headless, 
            args=_LAUNCH_ARGS + (["--headless=new"] if headless else [])
        )
    except Exception:
        pass

    # 3) Try explicit executable_path (env or arg or common paths)
    candidates = []
    if executable_path:
        candidates.append(executable_path)
    if os.environ.get("AA_CHROME"):
        candidates.append(os.environ["AA_CHROME"])
    candidates += _default_chrome_paths()

    for path in candidates:
        if Path(path).exists():
            try:
                return p.chromium.launch(executable_path=path, headless=headless, args=_LAUNCH_ARGS)
            except Exception:
                continue

    # If we’re here, nothing worked
    raise RuntimeError("Could not find a browser to launch. "
                       "Either run `python -m playwright install chromium` "
                       "or set AA_CHROME to your Chrome path.")

# ---------- Profile loader (YAML or JSON) ----------
def load_profile(path: str) -> AutoApplyProfile:
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"Profile file not found: {p}")
    text = p.read_text(encoding="utf-8")
    data: Dict[str, Any]
    if _HAS_YAML and (p.suffix.lower() in [".yml", ".yaml"]):
        data = yaml.safe_load(text) or {}
    else:
        data = json.loads(text or "{}")
    prof = AutoApplyProfile(
        full_name=str(data.get("full_name", "")),
        email=str(data.get("email", "")),
        phone=str(data.get("phone", "")),
        resume_path=str(data.get("resume_path", "")),
        location=(data.get("location") or None),
        cover_letter_path=(data.get("cover_letter_path") or None),
        linkedin_url=(data.get("linkedin_url") or None),
        github_url=(data.get("github_url") or None),
        portfolio_url=(data.get("portfolio_url") or None),
    )
    prof.validate()
    return prof


# ---------- Router ----------
def _guess_ats(url: str) -> str:
    if not url:
        return ""
    host = (urlparse(url).hostname or "").lower()
    if "lever.co" in host:
        return "lever"
    if "greenhouse.io" in host or "boards.greenhouse.io" in host:
        return "greenhouse"
    return ""


def auto_apply_headless(job_row: Dict[str, Any], profile: AutoApplyProfile, timeout_ms: int = 25000) -> AutoApplyResult:
    if not _HAS_PLAYWRIGHT:
        return AutoApplyResult(False, "playwright-not-installed", {"hint": "pip install playwright && playwright install"})

    raw_url = (job_row.get("canonical_apply_url") or job_row.get("apply_url") or "").strip()
    if not raw_url:
        return AutoApplyResult(False, "no-apply-url", {"job_id": job_row.get("job_id")})

    ats = (job_row.get("ats_type") or "").lower().strip()
    url = _resolve_final_url(raw_url)

    try:
        profile.validate()
    except Exception as e:
        return AutoApplyResult(False, "profile-invalid", {"error": str(e)})

    with sync_playwright() as p:
        browser = _launch_browser(p, headless=False)
        
        # Ensure video directory exists for debugging
        video_dir = Path("out/videos")
        video_dir.mkdir(parents=True, exist_ok=True)
        
        context = browser.new_context(
            record_video_dir=str(video_dir),
            record_video_size={'width': 1280, 'height': 720}
        )
        page = context.new_page()
        page.set_default_timeout(timeout_ms)

        # STEP 1: handle WWR / RSS / Unknown specifically
        if "weworkremotely.com" in url or ats in ("rss", "unknown", ""):
            try:
                page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
            except Exception:
                browser.close()
                return AutoApplyResult(False, "nav-timeout", {"url": url})
            try:
                loc = page.locator("a:has-text('Apply for this position')")
                if loc.count() > 0:
                    link = loc.first.get_attribute("href")
                    from urllib.parse import urljoin
                    link = urljoin(url, link)
                    url = _resolve_final_url(link)
                    ats = _guess_ats_from_url(url)
                    page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
            except Exception:
                browser.close()
                return AutoApplyResult(False, "no-apply-link", {"url": url})

        else:
            try:
                page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
            except Exception:
                browser.close()
                return AutoApplyResult(False, "nav-timeout", {"url": url})

        if ats == "lever":
            res = _lever_submit(page, url, profile)
        elif ats == "greenhouse":
            res = _greenhouse_submit(page, url, profile)
        elif ats == "workday":
            res = _workday_submit(page, url, profile)
        else:
            res = AutoApplyResult(False, "opened-manual", {"url": url, "ats_type": ats or "unknown"})

        # STEP 3: evidence
        try:
            png = page.screenshot()
            res.details.setdefault("evidence", {})["screenshot"] = bool(png)
        except Exception:
            pass
        res.details.setdefault("evidence", {})["final_url"] = page.url

        browser.close()
        return res




def _gemini_decide(context_text: str, profile: AutoApplyProfile, options: list = None) -> str:
    import json, yaml
    from google import genai
    from dataclasses import asdict
    try:
        with open("config/config.yaml", "r", encoding="utf-8") as f:
            c = yaml.safe_load(f)
        if not c.get("gemini_api_key"): return ""
        client = genai.Client(api_key=c["gemini_api_key"])
        model_id = c.get("gemini_model", "gemini-2.0-flash-lite")
        if model_id.startswith("models/"): model_id = model_id.replace("models/", "")
        pdict = asdict(profile)
        if options:
            q = f"Applicant details: {json.dumps(pdict)}\nQuestion context: {context_text}\nOptions available: {options}\nWhich option is best? Reply ONLY with the exact option text. If nothing matches, return the closest option."
        else:
            q = f"Applicant details: {json.dumps(pdict)}\nQuestion context: {context_text}\nAnswer this concisely (1-3 words max). Return ONLY the answer."
        res = client.models.generate_content(model=model_id, contents=q)
        return res.text.strip().replace('"', '').replace("'", "")
    except:
        return ""

# ---------- Common helpers ----------
def _fill_any(page, selectors: list[str], value: str):
    for sel in selectors:
        try:
            loc = page.locator(sel)
            if loc.count() > 0 and loc.first.is_visible():
                loc.first.fill(value)
                return True
        except Exception:
            continue
    return False

def _set_file_any(page, selectors: list[str], file_path: str):
    for sel in selectors:
        try:
            loc = page.locator(sel)
            if loc.count() > 0 and loc.first.is_visible():
                loc.first.set_input_files(file_path)
                return True
        except Exception:
            continue
    # fallback: first file input
    try:
        loc = page.locator("input[type='file']")
        if loc.count() > 0 and loc.first.is_visible():
            loc.first.set_input_files(file_path)
            return True
    except Exception:
        pass
    return False

def _check_all(target, selectors: list[str]):
    for sel in selectors:
        for loc in target.locator(sel).all():
            try:
                if not loc.is_checked(): loc.evaluate("el => el.click()")
            except: pass

_SUBMIT_BTNS = [
    "button:has-text('Submit application')",
    "button:has-text('Submit Application')",
    "button:has-text('Submit')",
    "button:has-text('Apply now')",
    "button:has-text('Apply Now')",
    "button:has-text('Apply')",
    "input[type='submit']",
]

def _click_submit_and_confirm(page) -> bool:
    clicked = False
    for sel in _SUBMIT_BTNS:
        try:
            loc = page.locator(sel)
            if loc.count() > 0 and loc.first.is_visible():
                loc.first.click()
                clicked = True
                break
        except Exception:
            continue
    if not clicked:
        return False
    # wait for possible confirmation
    try:
        page.wait_for_load_state("networkidle", timeout=8000)
    except Exception:
        pass
    try:
        html = page.content().lower()
        return any(x in html for x in [
            "thank you for applying",
            "application submitted",
            "we received your application",
            "thanks for your application",
            "submission received",
        ])
    except Exception:
        return False


# ---------- Lever ----------
def _lever_submit(page, url: str, profile: AutoApplyProfile) -> AutoApplyResult:
    # some Lever pages open a modal/application panel
    try:
        page.locator("a:has-text('Apply for this job'), button:has-text('Apply for this job')").first.click(timeout=3000)
    except Exception:
        pass

    _fill_any(page, ["input[name='name']", "input[placeholder*='name' i]"], profile.full_name)
    _fill_any(page, ["input[name='email']", "input[placeholder*='email' i]"], profile.email)
    _fill_any(page, ["input[name='phone']", "input[placeholder*='phone' i]", "input[type='tel']"], profile.phone)

    _set_file_any(page, ["input[type='file'][name*='resume' i]", "input[type='file']"], profile.resume_path)

    if profile.cover_letter_path:
        _set_file_any(page, ["input[type='file'][name*='cover' i]"], profile.cover_letter_path)
    if profile.linkedin_url:
        _fill_any(page, ["input[name*='linkedin' i]", "input[placeholder*='linkedin' i]"], profile.linkedin_url)
    if profile.github_url:
        _fill_any(page, ["input[name*='github' i]", "input[placeholder*='github' i]"], profile.github_url)
    if profile.portfolio_url:
        _fill_any(page, ["input[name*='portfolio' i]", "input[name*='website' i]", "input[placeholder*='portfolio' i]"], profile.portfolio_url)

    _check_all(page, ["input[type='checkbox']"])

    submitted = _click_submit_and_confirm(page)
    status = "lever-submitted" if submitted else "lever-submission-unknown"
    return AutoApplyResult(submitted, status, {"url": url, "profile": asdict(profile)})


# ---------- Greenhouse ----------
def _greenhouse_submit(page, url: str, profile: AutoApplyProfile) -> AutoApplyResult:
    # If embedded, point a FrameLocator at it; else fall back to page
    target = page
    try:
        if page.frame_locator("iframe").count() > 0:
            target = page.frame_locator("iframe").first
    except Exception:
        pass

    # Use target.locator(...) instead of page.get_by_label(...) to be generic
    first = profile.full_name.split()[0]
    last  = " ".join(profile.full_name.split()[1:]) or profile.full_name

    # Names (try labeled inputs; fall back to name/placeholder heuristics)
    try:
        target.get_by_label("First name", exact=False).fill(first)
        target.get_by_label("Last name", exact=False).fill(last)
    except Exception:
        _fill_any(target, ["input[name*='first' i]"], first)
        _fill_any(target, ["input[name*='last' i]"], last)

    _fill_any(target, ["input[type='email']", "input[name*='email' i]"], profile.email)
    _fill_any(target, ["input[type='tel']", "input[name*='phone' i]"], profile.phone)

    _set_file_any(target, ["input[type='file'][name*='resume' i]", "input[type='file']"], profile.resume_path)

    if profile.cover_letter_path:
        _set_file_any(target, ["input[type='file'][name*='cover' i]"], profile.cover_letter_path)
    if profile.linkedin_url:
        _fill_any(target, ["input[name*='linkedin' i]", "input[placeholder*='linkedin' i]"], profile.linkedin_url)
    if profile.github_url:
        _fill_any(target, ["input[name*='github' i]"], profile.github_url)
    if profile.portfolio_url:
        _fill_any(target, ["input[name*='portfolio' i]", "input[name*='website' i]"], profile.portfolio_url)

    _check_all(target, ["input[type='checkbox']"])

    # ---------- AI AnswerEngine for Custom Questions ----------
    try:
        # Greenhouse custom questions typically live inside div.field, div.custom_question, or div.select__container
        # We iterate over all visible labels
        labels = target.locator("label:visible").all()
        for label_loc in labels:
            label_text = label_loc.text_content().strip()
            
            # Since we iterate over labels, we redefine `field` as the parent of the label
            field = label_loc.locator("..")
            
            # 0. City Location Handler (Google Maps Autocomplete)
            if "location" in label_text.lower() and "city" in label_text.lower():
                try:
                    loc_input = field.locator("input[type='text']:visible, input[role='combobox']:visible").first
                    if loc_input.count() > 0:
                        loc_input.fill("San Francisco, California") # Fallback explicit city format
                        target.wait_for_timeout(1000)
                        target.keyboard.press("ArrowDown")
                        target.wait_for_timeout(200)
                        target.keyboard.press("Enter")
                except: pass
                continue
                
            if not label_text or "*" not in label_text: 
                # If you want to answer non-mandatory questions too, remove the "*" check. Currently only doing it for mandatory to save tokens.
                if "gender" not in label_text.lower() and "race" not in label_text.lower() and "veteran" not in label_text.lower() and "disability" not in label_text.lower():
                    # Only skip if it's not a standard EE question
                    if "*" not in label_text: continue
                
            parent = label_loc.locator("..")
            
            # 1. Select boxes (Native HTML Dropdowns)
            select_loc = parent.locator("select:visible").first
            if select_loc.count() == 0:
                # try grandparent
                select_loc = parent.locator("..").locator("select:visible").first
                
            if select_loc.count() > 0:
                current_val = select_loc.evaluate("el => el.value")
                if not current_val or current_val == "":
                    options_elements = select_loc.locator("option").all()
                    opts = [o.text_content().strip() for o in options_elements if o.text_content().strip() and "please select" not in o.text_content().lower()]
                    if opts:
                        ans = _gemini_decide(label_text, profile, options=opts)
                        if ans:
                            # To be safe, try to select by exactly matching
                            try: select_loc.select_option(label=ans)
                            except: pass
                            
            # 2. React-Select Comboboxes (Modern Greenhouse)
            combo = parent.locator('input[role="combobox"]:visible').first
            if combo.count() == 0:
                combo = parent.locator('..').locator('input[role="combobox"]:visible').first
                
            if combo.count() > 0:
                current_val = combo.evaluate("el => el.value")
                if not current_val:
                    try:
                        combo.click(force=True)
                        target.wait_for_timeout(300)
                        opts = target.locator('div[role="option"]').all_text_contents()
                        if opts:
                            ans = _gemini_decide(label_text, profile, options=opts)
                            print(f"[DEBUG] LABEL: {label_text} | OPTS: {opts[:2]}... | ANS: {ans}")
                            if ans:
                                # Try clicking the actual option div first (safest and most reliable)
                                try:
                                    import re
                                    def _norm(s): return re.sub(r'[^a-z0-9]', '', s.lower())
                                    a_norm = _norm(ans)
                                    opt_matches = [i for i, text in enumerate(opts) if a_norm and a_norm in _norm(text)]
                                    if not opt_matches:
                                        # fallback check if any option is a substring of the answer
                                        opt_matches = [i for i, text in enumerate(opts) if _norm(text) and _norm(text) in a_norm]
                                        
                                    if opt_matches:
                                        print(f"[DEBUG] Clicking exact match {opt_matches[0]} for {ans}")
                                        target.locator('div[role="option"]').nth(opt_matches[0]).click(force=True)
                                    else:
                                        combo.fill(ans)
                                        target.wait_for_timeout(300)
                                        target.keyboard.press("Enter")
                                        target.keyboard.press("Escape")
                                except: pass
                        else:
                            ans = _gemini_decide(label_text, profile)
                            if ans:
                                combo.fill(ans)
                                target.wait_for_timeout(300)
                                target.keyboard.press("Enter")
                                target.keyboard.press("Escape")
                    except Exception as e:
                        print(f"[Greenhouse] Combobox error: {e}")
                        
            # 3. Custom text inputs
            text_loc = parent.locator("input[type='text']:visible, textarea:visible").first
            if text_loc.count() == 0:
                text_loc = parent.locator('..').locator("input[type='text']:visible, textarea:visible").first
                
            if text_loc.count() > 0 and text_loc.get_attribute("role") != "combobox":
                try:
                    # Ignore standard fields we already filled
                    name_attr = text_loc.get_attribute("name") or ""
                    if "first" in name_attr.lower() or "last" in name_attr.lower() or "email" in name_attr.lower() or "phone" in name_attr.lower():
                        continue
                        
                    current_val = text_loc.evaluate("el => el.value")
                    if not current_val:
                        ans = _gemini_decide(label_text, profile)
                        if ans: text_loc.fill(ans)
                except: pass
    except Exception as e:
        print(f"[Greenhouse] AnswerEngine error: {e}")
        pass
    # --------------------------------------------------------

    submitted = _click_submit_and_confirm(target)
    status = "greenhouse-submitted" if submitted else "greenhouse-submission-unknown"
    return AutoApplyResult(submitted, status, {"url": url, "profile": asdict(profile)})

# ---------- Workday ----------
def _workday_submit(page, url: str, profile: AutoApplyProfile) -> AutoApplyResult:
    import time
    from google import genai
    import yaml
    import json
    from dataclasses import asdict
    def _local_gemini_decide(context_text: str, options: list = None) -> str:
        return _gemini_decide(context_text, profile, options)


    try: page.locator('button:has-text("Accept Cookies")').first.evaluate("el => el.click()")
    except: pass
    
    try: page.locator('[data-automation-id="adventureButton"]').first.evaluate("el => el.click()")
    except: pass
    
    try: page.locator('[data-automation-id="autofillWithResume"]').first.evaluate("el => el.click()")
    except: pass

    try: page.locator('div:has-text("Sign in with email"), button:has-text("Sign in with email")').last.evaluate("el => el.click()")
    except: pass
    
    def _workday_perform_login():
        try:
            print(f"[Workday] Attempting login for {email}...")
            # Try to find email input directly (must be visible)
            email_inner_sel = 'input[data-automation-id="email"]:visible, input[type="email"]:visible'
            email_f = page.locator(email_inner_sel).first
            
            if email_f.count() == 0:
                # Try to click "Sign In" to open the overlay
                for sel in ['div:has-text("Sign in with email")', 'button:has-text("Sign in with email")', 'button:has-text("Sign In")', 'a:has-text("Sign In")']:
                    try: 
                        loc = page.locator(sel).last
                        if loc.count() > 0 and loc.is_visible():
                            loc.evaluate("el => el.click()")
                            time.sleep(2) # Wait for overlay animation
                            break
                    except: pass
                email_f = page.locator(email_inner_sel).first

            if email_f.count() > 0:
                email_f.fill(email, timeout=5000)
                pwd_f = page.locator('input[data-automation-id="password"]:visible, input[type="password"]:visible').first
                pwd_f.fill(pwd, timeout=5000)
                
                # The Sign In button in the overlay
                submit_btn = page.locator('button[data-automation-id="signInSubmitButton"]:visible, button:has-text("Sign In"):visible').first
                submit_btn.evaluate("el => el.click()")
                time.sleep(3)
                
                # Success if overlay is gone or URL changed
                return "sign in" not in page.content().lower() and "invalid" not in page.content().lower()
        except: pass
        return False

    email = profile.email
    pwd = "AmoghJobs2025!" 
    
    # PHASE 1: Try Log In
    is_authenticated = _workday_perform_login()
    
    # PHASE 2: Create Account (only if Login failed or was never found)
    if not is_authenticated:
        print("[Workday] Not authenticated. Attempting 'Create Account' fallback...")
        try:
            # Search for Create Account link
            create_acc_loc = page.locator('a:has-text("Create Account"):visible, a:has-text("Create an Account"):visible, button:has-text("Create Account"):visible').first
            if create_acc_loc.count() > 0:
                create_acc_loc.evaluate("el => el.click()")
                time.sleep(3)
                
                print(f"[Workday] Account Creation Page: {page.url}")
                # Fill Email and Password
                try:
                    page.locator('input[data-automation-id="email"], input[type="email"]').first.fill(email, timeout=5000)
                    page.locator('input[data-automation-id="password"], input[type="password"]').first.fill(pwd, timeout=5000)
                    
                    # Verify Password
                    v_pass = page.locator('input[data-automation-id="verifyPassword"], input[data-automation-id="confirmPassword"], input[data-automation-id="reenterPassword"]').first
                    if v_pass.count() == 0:
                        v_pass = page.get_by_label(re.compile(r"Verify|Confirm|Re-enter", re.I)).first
                    if v_pass.count() == 0:
                        v_pass = page.locator('input[type="password"]:visible').nth(1)
                    
                    if v_pass.count() > 0:
                        v_pass.fill(pwd, timeout=5000)
                    
                    # Consent Checkbox
                    expanders = ['[data-automation-id="readMore"]', 'button:has-text("Read More")', '[role="button"]:has-text("Read More")', 'svg[data-icon="chevron-down"]', 'div:has-text("Read More")']
                    for ex in expanders:
                        loc = page.locator(ex).first
                        if loc.count() > 0 and loc.is_visible():
                            loc.evaluate("el => el.click()")
                            time.sleep(1)
                            break
                            
                    cb = page.locator('[data-automation-id="agreementCheckbox"], [data-automation-id="agreement"], input[type="checkbox"]').first
                    if cb.count() > 0: cb.evaluate("el => el.click()")
                    else: page.locator('label:has-text("read and consent"), label:has-text("I agree"), label:has-text("I have read")').first.evaluate("el => el.click()")
                    
                    # Final Create Account click
                    btn = page.locator('button[data-automation-id="createAccountSubmitButton"], [data-automation-id="click_filter"]:has-text("Create Account")').last
                    btn.evaluate("el => el.click()")
                    print("[Workday] Account creation submitted. Waiting for transition...")
                    time.sleep(5) # Give it more time to process
                    
                    # One last check: if we are back on Sign In or see an error, it might have actually worked or account existed
                    content = page.content().lower()
                    if "already exists" in content or ("sign in" in content and "create" not in page.url.lower()):
                        print("[Workday] Account definitely exists or redirected to Sign In. Finalizing session with Login...")
                        _workday_perform_login()
                except Exception as e:
                    print(f"[Workday] Account creation error: {e}")
            else:
                print("[Workday] No 'Create Account' option found.")
        except Exception as e:
            print(f"[Workday] Fatal error in authentication flow: {e}")

    # PHASE 3: Ensure we are actually logged in before questionnaire
    time.sleep(3)
    # If still on Sign In or Create Account page after everything, we are stuck
    final_content = page.content().lower()
    if ("sign in" in final_content or "create account" in final_content) and "my information" not in final_content:
        # One last desperate login attempt if we see fields
        if page.locator('input[type="email"]:visible').count() > 0:
            print("[Workday] Detecting stuck at login. Final desperate login attempt...")
            _workday_perform_login()
            time.sleep(3)
    
    # Check again. If still stuck, we might fail Phase 3
    if ("sign in" in page.content().lower() or "create account" in page.content().lower()) and "my information" not in page.content().lower():
        print("[Workday] WARNING: Still appears to be on auth screen. Proceeding to Resume Upload but may fail.")

    # PHASE 4: Resume Upload and Application questionnaire
    try:
        # Check if we are on the 'Autofill with Resume' page and need to upload
        if "autofill" in page.url.lower() or "resume" in page.content().lower():
            file_input = page.locator('input[type="file"]').first
            if file_input.count() > 0:
                print("[Workday] Found resume upload field. Uploading...")
                file_input.set_input_files(profile.resume_path)
                time.sleep(4)
                # Click Continue after upload
                cont_btn = page.locator('button:has-text("Continue"), [data-automation-id="bottom-navigation-next-button"]').first
                if cont_btn.count() > 0:
                    cont_btn.evaluate("el => el.click()")
                    time.sleep(3)
    except Exception as e:
        print(f"[Workday] Resume upload error: {e}")
    
    time.sleep(3)

    try:
        page.locator('input[type="file"]').first.set_input_files(profile.resume_path)
        time.sleep(3)
    except: pass

    for _ in range(12):
        time.sleep(2)
        html = page.content().lower()
        if "application submitted" in html or "thank you" in html:
            print("[Workday] Application submitted successfully!")
            return AutoApplyResult(True, "workday-submitted", {"url": url, "profile": asdict(profile)})
            
        print(f"[Workday] Step {_ + 1}: Analyzing page {page.url}...")
        
        # Helper to fill fields (refactored for re-scanning)
        def fill_all_visible_fields():
            fields = page.locator('[data-automation-id="formField"], [data-automation-id="questionField"]').all()
            if not fields:
                fields = page.locator('div:has(> label), .wd-Field, .css-1n0y0j5').all()
            
            filled_any = False
            for field in fields:
                try:
                    # Find the actual interactive element
                    clickable = field.locator('input, [role="combobox"], [role="radiogroup"], [role="radio"], textarea, select').first
                    if clickable.count() == 0: continue
                    
                    # Capture label and value
                    label_text = field.text_content() or ""
                    val = ""
                    try: val = clickable.input_value()
                    except: pass
                    
                    # Skip if already filled and seems valid
                    if val and len(val) > 1: continue

                    print(f"[Workday]   Field detected: '{label_text[:30].strip()}...'")
                    
                    tag = clickable.evaluate("el => el.tagName").lower()
                    ctype = clickable.get_attribute("type")
                    role = clickable.get_attribute("role")

                    # DATE FIELDS (MM/DD/YYYY)
                    if "date" in label_text.lower() or "day" in label_text.lower() or "year" in label_text.lower():
                        # Try to fill numeric or date inputs
                        if tag == "input":
                            ans = _local_gemini_decide(label_text + " (it's a date field component)")
                            clickable.fill(ans)
                            filled_any = True
                            continue

                    # INPUTS / TEXTAREAS
                    if (tag in ["input", "textarea"] and (ctype in ["text", "tel", "email"] or not ctype)) or tag == "select":
                        if ctype == "tel": clickable.fill(profile.phone or "2123481111")
                        elif ctype == "email": email_val = profile.email; clickable.fill(email_val)
                        else:
                            ans = _local_gemini_decide(label_text)
                            if ans: 
                                if tag == "select": clickable.select_option(label=ans)
                                else: clickable.fill(ans)
                                filled_any = True
                            elif not val: 
                                clickable.fill(profile.full_name)
                                filled_any = True

                    # COMBOBOXES (Dropdowns)
                    elif role == "combobox":
                        clickable.evaluate("el => el.click()")
                        time.sleep(1.0) # Wait for options to populate
                        opts_loc = page.locator('[role="option"]')
                        opts = opts_loc.all_inner_texts()
                        if len(opts) > 0:
                            # Contextual hint for Language Proficiency
                            context = label_text
                            if "language" in label_text.lower() and "rating" not in label_text.lower():
                                context += " (Select 'English' or 'Fluent' if applicable)"
                            
                            ans = _local_gemini_decide(context, options=opts)
                            if ans:
                                for idx, opt_text in enumerate(opts):
                                    if ans.lower() in opt_text.lower():
                                        page.locator('[role="option"]').nth(idx).evaluate("el => el.click()")
                                        filled_any = True
                                        break
                                if not filled_any: 
                                    page.locator('[role="option"]').first.evaluate("el => el.click()")
                            else:
                                page.locator('[role="option"]').first.evaluate("el => el.click()")
                        else:
                            clickable.press("Escape")

                    # RADIOS
                    elif role in ["radiogroup", "radio"] or ctype == "radio":
                        ans = _local_gemini_decide(label_text, options=["Yes", "No", "Prefer not to say"])
                        if ans: 
                            r_loc = field.locator(f'label:has-text("{ans}")').first
                            if r_loc.count() > 0:
                                r_loc.evaluate("el => el.click()")
                                filled_any = True
                            else:
                                radios = field.locator('label, [role="radio"]').all()
                                if len(radios) > 0: 
                                    radios[0].evaluate("el => el.click()")
                                    filled_any = True
                except: pass
            return filled_any

        # PROACTIVE FILLING: Initial pass
        if fill_all_visible_fields():
            time.sleep(1) # Wait for dynamic fields to appear
            fill_all_visible_fields() # Second pass for newly spawned fields

        # Special case: mandatory non-standard fields (Referrals/Demographics)
        for label in ["Job Boards", "LinkedIn", "No", "Email", "Self-Identified"]:
            try: 
                loc = page.locator(f'label:has-text("{label}")').first
                if loc.count() > 0 and loc.is_visible():
                    loc.evaluate("el => el.click()")
            except: pass

        # Move to next page
        try: 
            found_next = False
            # Labeled 'Submit' on last page, 'Continue' or 'Next' elsewhere
            for sel in ['[data-automation-id="bottom-navigation-next-button"]', 'button:has-text("Submit")', 'button:has-text("Continue")', 'button:has-text("Next")']:
                btn = page.locator(sel).first
                if btn.count() > 0 and btn.is_visible():
                    print(f"[Workday] Advancing to next step: {sel}")
                    btn.evaluate("el => el.click()")
                    found_next = True
                    break
            
            if not found_next:
                # Try a final submit button search if it's the review page
                sbmt = page.locator('button:has-text("Submit")').first
                if sbmt.count() > 0:
                    sbmt.evaluate("el => el.click()")
        except:
            pass
            
    return AutoApplyResult(False, "workday-timeout", {"url": url, "profile": asdict(profile)})

