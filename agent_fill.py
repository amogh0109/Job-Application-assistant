from playwright.sync_api import sync_playwright
from app.auto_apply import load_profile
import time

def fill():
    p = sync_playwright().start()
    b = p.chromium.launch(headless=False) # Visibly showing it
    page = b.new_page()
    page.goto('https://job-boards.greenhouse.io/twilio/jobs/7066029')
    page.wait_for_load_state('networkidle')
    prof = load_profile('config/auto_apply_profile.yaml')
    
    print('Filling text fields...')
    page.locator('#first_name').fill(prof.full_name.split()[0])
    page.locator('#last_name').fill(' '.join(prof.full_name.split()[1:]))
    page.locator('#email').fill(prof.email)
    page.locator('#phone').fill(prof.phone)
    
    print('Uploading Resume...')
    page.locator('input[type="file"]').first.set_input_files(prof.resume_path)
    
    try:
        print('Filling location...')
        loc_box = page.locator('#candidate-location')
        loc_box.fill('San Francisco, California, United States')
        time.sleep(1)
        page.keyboard.press('ArrowDown')
        time.sleep(0.5)
        page.keyboard.press('Enter')
    except: pass
    
    try:
        print('Filling LinkedIn...')
        page.locator('label:has-text("LinkedIn")').locator('..').locator('input[type="text"]').first.fill(prof.linkedin_url or '')
    except: pass

    def select_dropdown(search_text, answer):
        try:
            parent = page.locator(f'label:has-text("{search_text}")').locator('..')
            combo = parent.locator('input[role="combobox"]:visible').first
            if combo.count() == 0: combo = page.locator(f'input[role="combobox"][aria-labelledby*="{search_text}" i]')
            if combo.count() > 0:
                combo.first.click(force=True)
                time.sleep(0.5)
                opts = page.locator('div[role="option"]').all()
                for o in opts:
                    if answer.lower() in o.text_content().lower():
                        o.click(force=True)
                        break
                page.keyboard.press('Escape')
        except: pass

    print('Answering Custom Guidelines...')
    select_dropdown('legally authorized', 'Yes')
    select_dropdown('sponsorship', 'No')
    select_dropdown('Cuba', 'No')
    
    print('Answering EEOC Defaults...')
    for q_id, ans in [('#1712', "don't wish"), ('#1713', 'Male'), ('#1714', 'Asian'), ('#1715', "don't wish"), ('#1716', "don't wish")]:
        try:
            page.locator(q_id).first.click(force=True)
            time.sleep(0.5)
            opts = page.locator('div[role="option"]').all()
            for o in opts:
                if ans.lower() in o.text_content().lower().replace('’', "'"):
                    o.click(force=True)
                    break
            page.keyboard.press('Escape')
        except: pass

    print('Checking mandatory boxes...')
    for chk in page.locator('input[type="checkbox"]').all():
        if not chk.is_checked(): chk.evaluate('el => el.click()')
        
    print('Done! Clicking Submit...')
    page.locator('button:has-text("Submit application")').click()
    time.sleep(5)
    b.close()
    p.stop()

if __name__ == "__main__":
    fill()
