from playwright.sync_api import sync_playwright
import time
import os

def check_mandatory_and_fill(page, email, pwd, attempt=0):
    if attempt > 10: return

    try: page.locator('[data-automation-id="bottom-navigation-next-button"]').click(timeout=3000)
    except:
        try: page.locator('button:has-text("Submit")').click(timeout=3000)
        except: pass

    time.sleep(2)
    invalids = page.locator('[aria-invalid="true"]')
    if invalids.count() == 0: return 

    for i in range(invalids.count()):
        try:
            el = invalids.nth(i)
            tag = el.evaluate("el => el.tagName").lower()
            ctype = el.get_attribute("type")
            role = el.get_attribute("role")

            if tag == "input" and ctype in ["text", "tel", "email"]:
                val = el.input_value()
                if "555" in val or "Amogh" in val: continue
                if ctype == "tel": el.fill("2123481111")
                elif ctype == "email": el.fill(email)
                else: el.fill("Amogh")

            if ctype == "checkbox":
                el.check(force=True)
                
            if role == "combobox":
                el.click()
                time.sleep(0.5)
                page.locator('[role="option"]').nth(1).click()

        except: pass
            
    # Answer mandatory radios
    for label in ["Job Boards", "No", "Prefer not to say"]:
        try: page.locator(f'label:has-text("{label}")').first.click(timeout=500)
        except: pass

    check_mandatory_and_fill(page, email, pwd, attempt+1)


def run():
    print("Launching AI Auto-Apply (Starting Fresh)...")
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False, args=['--window-size=1200,900'])
        context = browser.new_context(viewport={'width': 1200, 'height': 900})
        page = context.new_page()

        job_url = "https://logitech.wd5.myworkdayjobs.com/en-US/Logitech/job/Lausanne-Switzerland/Sr-AI-ML-Engineer--Video--R-D-_144373?q=AI"
        page.goto(job_url)

        try: page.locator('button:has-text("Accept Cookies")').click(timeout=3000)
        except: pass
        
        page.locator('[data-automation-id="adventureButton"]').first.click()
        page.locator('[data-automation-id="autofillWithResume"]').click()
        
        try: page.locator('div:has-text("Sign in with email")').last.click(timeout=3000)
        except: pass

        email = "amoghjobs25@gmail.com"
        pwd = "AmoghJobs2025!"

        print("Executing Login...")
        page.locator('input[type="email"]').first.fill(email)
        page.locator('input[type="password"]').first.fill(pwd)
        page.locator('div[role="button"]:has-text("Sign In")').click()
        
        try:
            page.wait_for_selector('text="Invalid user name or password"', timeout=3000)
            page.locator('button:has-text("Create Account")').click()
            time.sleep(1)
            page.locator('input[type="email"]').first.fill(email)
            passes = page.locator('input[type="password"]')
            passes.nth(0).fill(pwd)
            if passes.count() > 1: passes.nth(1).fill(pwd)
            page.locator('[type="checkbox"]').check(force=True)
            page.locator('button:has-text("Create Account")').last.click()
        except: pass
            
        page.wait_for_load_state('networkidle')
        time.sleep(3)

        # Use the exact path requested by user!
        resume_path = r"C:\Users\Amogh Wyawahare\Desktop\Resumes\AI Resume\Resume_AI.pdf"
        print(f"Uploading Resume: {resume_path}")
        
        if os.path.exists(resume_path):
            page.locator('input[type="file"]').first.set_input_files(resume_path)
            time.sleep(3)
        else:
            print(f"ERROR: Resume not found at {resume_path}")

        print("Iterating through application logic...")
        for current_page in range(10):
            time.sleep(2)
            html = page.content().lower()
            if "application submitted" in html or "thank you" in html:
                print("🎉 APPLICATION SUCCESSFULLY SUBMITTED! 🎉")
                break
                
            check_mandatory_and_fill(page, email, pwd)
            
        browser.close()

if __name__ == "__main__":
    run()
