# Apply Bot Plan

## 0. Goal
Input: list of 10 job application URLs plus one candidate profile.  
Output: each application form is parsed, questions detected (text + dropdowns + selections), AI generates answers, form is filled and optionally submitted, and everything is logged.

## 1. High-Level Architecture
Core components:
1) BatchOrchestrator
2) BrowserController (Playwright)
3) JobContextBuilder
4) Gemini Brain (main driver: page state + actions + fields)
5) PageAnalyzer
6) QuestionBlockExtractor
7) AnswerEngine (Rules + LLM)
8) FormFiller
9) NavigationController + ATS Flow Registry (per-ATS adapters + generic fallback)
10) Logger & Storage
11) Config/Profile Manager

## 2. Inputs & Outputs
### Inputs
- profile.json: static user info (name, email, phone, links, work auth, sponsorship, years experience, preferences, etc.)
- job_links.txt or Python list of job URLs.
- config.yaml: mode (review|auto_submit), max_jobs_per_run, logging options, LLM model/params, prefer_not_to_say behavior, gemini_api_key/model.

### Outputs (per job URL)
Application result record:
```json
{
  "job_url": "...",
  "timestamp": "...",
  "status": "submitted" | "review_pending" | "failed",
  "confirmation_text": "...",
  "questions": [
    {
      "text": "...",
      "field_type": "textarea",
      "answer": "...",
      "options": null,
      "source": "ai"
    }
  ],
  "error": null
}
```
Optional: HTML snapshot / screenshot paths.

## 3. Core Data Models (Python-ish)
```python
class Profile(BaseModel):
    full_name: str
    email: str
    phone: str
    location: str
    linkedin_url: str | None
    github_url: str | None
    work_auth: bool
    sponsorship_needed: bool
    total_years_experience: float
    years_by_skill: dict[str, float]
    preferred_location_type: str
    demographic_preferences: str
    resume_path: str
    cover_letter_template_path: str | None

class JobPosting(BaseModel):
    url: str
    title: str | None
    company: str | None
    location: str | None
    description_html: str | None
    description_text: str | None

class Option(BaseModel):
    label: str
    locator: str

class QuestionBlock(BaseModel):
    locator: str
    field_type: str  # text, textarea, select, radio, checkbox, file, date
    question_text: str
    options: list[Option] | None
    multiple: bool = False

class ApplicationResult(BaseModel):
    job: JobPosting
    status: str
    questions: list[dict]
    confirmation_text: str | None
    error: str | None
    timestamp: str
```

## 4. Component Responsibilities
### 4.1 BatchOrchestrator
Input: job URLs, Profile, Config. Output: list[ApplicationResult]. Loops jobs, builds JobPosting via JobContextBuilder, runs SingleJobFlow, collects results.

### 4.2 BrowserController
Playwright wrapper: launches browser (persistent if reusing login), new page per job, helpers: goto, get_dom_snapshot, take_screenshot, find_and_click_button_by_text, extract_visible_text.

### 4.3 JobContextBuilder
Goal: extract job info for context/prompts. Navigate to URL, click Apply if needed, scrape title/company/location/description HTML/text. Return JobPosting.

### 4.4 Gemini Brain
Primary driver: send DOM snapshots to Gemini to classify page state and emit an ordered action plan (click/fill/select/check/file) with target hints and fields. Loop: Gemini plan → execute with Playwright → repeat until confirmation/review/blocked.

### 4.5 PageAnalyzer
Decides if page is application form, review/confirmation, or intermediate. Methods: is_confirmation_page(), has_questions().

### 4.6 QuestionBlockExtractor
Goal: extract list[QuestionBlock] from page. Find inputs/textarea/select/radio/checkbox/file/date; find nearby text (label/parent/siblings); determine field_type; collect options for select/radio/checkbox; return QuestionBlocks.

### 4.7 AnswerEngine (Rules + LLM)
Goal: given QuestionBlock, Profile, JobPosting, return (answer, source: rule|ai).
- text/textarea: rule mappings for known fields; else llm_answer_free_text.
- select/radio/checkbox: rule_based_choice (work auth, sponsorship, demographic, location type, experience); else llm_choose_option (index-based).
- file: map to resume/cover letter.
- date: availability/start date decisions.
LLM calls deterministic/structured; dropdown returns indices; text answers 3-6 sentences, no fabrication.

### 4.8 FormFiller
Goal: fill page elements per QuestionBlock+answer. text/textarea: fill; select: select_option by label; radio: click label similarity; checkbox: single/multi; file: set_input_files; date: format and fill. fill_all iterates blocks+answers.

### 4.9 NavigationController + ATS Flow Registry
Goal: click through multi-step forms until confirmation or stop. Loop is Gemini-led actions executed via Playwright (click/fill/select/check/file). Login/account gates: try sign-in first with profile email/password; if that fails, create account with same creds. Steps per page: Gemini analyze state/fields -> execute actions -> detect confirmation or error. In review mode, stop before final submit (status=review_pending). In auto_submit, continue to confirmation. Registry: map ATS id to flow adapter (LeverFlow, GreenhouseFlow, WorkdayFlow, AshbyFlow, SmartRecruitersFlow, GenericFlow). Pick via `_guess_ats_from_url` and optional DOM sniff. Non-implemented flows delegate to GenericFlow.

### 4.10 Logger & Storage
Persist job URL, JobPosting summary, all QuestionBlocks+answers, status, confirmation text, screenshot path, error trace. Storage options: JSON per run or SQLite tables (jobs, applications, questions, errors).

### 4.11 Config/Profile Manager
Loads profile.json and config.yaml, validates required fields, provides simple API: config.mode, max_jobs_per_run, llm_model, log_dir, gemini_api_key/model, profile fields, etc. Include workday_password for reuse across login/account creation.

## 5. End-to-End Flow (Single Job URL)
1) BatchOrchestrator picks URL.  
2) BrowserController opens page.  
3) Gemini analyzes DOM to choose actions (apply clicks, login/account creation, field list).  
4) Navigation executes Gemini actions via Playwright; fills fields (AnswerEngine/FormFiller); uses ATS adapters as needed; repeats until confirmation or failure.  
5) On confirmation: extract confirmation text, screenshot, build ApplicationResult(status="submitted").  
6) Close job tab.

## 6. Modes
- review: fill all fields, stop before final submit, status=review_pending.  
- auto_submit: fully automatic to confirmation.

## 7. One-Liner Summary to Devs
Implement these modules and data models exactly; keep ATS-agnostic; DOM logic is generic; question understanding and answering flow through QuestionBlock + AnswerEngine + FormFiller.
