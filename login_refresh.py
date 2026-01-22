#!/usr/bin/env python3
"""
Quick login refresh for Canvas session.
Opens a browser window - complete login there, then press Enter in terminal.
"""

import asyncio
import sys
from pathlib import Path
from playwright.async_api import async_playwright

CANVAS_URL = "https://canvas.santarosa.edu"
SESSION_FILE = Path(__file__).parent / ".canvas_session.json"

async def main():
    print("\n" + "="*60)
    print("Canvas Session Refresh")
    print("="*60)
    
    playwright = await async_playwright().start()
    
    # Load existing session if available
    storage_state = None
    if SESSION_FILE.exists():
        storage_state = str(SESSION_FILE)
        print(f"📂 Loading existing session from {SESSION_FILE.name}")
    
    browser = await playwright.chromium.launch(
        headless=False,
        args=['--disable-blink-features=AutomationControlled']
    )
    
    context = await browser.new_context(
        user_agent='Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36',
        viewport={'width': 1280, 'height': 720},
        storage_state=storage_state
    )
    
    page = await context.new_page()
    page.set_default_timeout(60000)
    
    print("\n🌐 Opening Canvas...")
    await page.goto(f"{CANVAS_URL}/courses")
    await page.wait_for_load_state("domcontentloaded")
    await asyncio.sleep(3)
    
    # Check if logged in
    current_url = page.url
    if "canvas.santarosa.edu" in current_url and "login" not in current_url.lower():
        dashboard = await page.query_selector('.ic-DashboardCard, #dashboard, table.course-list-table, .course-list')
        if dashboard:
            print("✅ Already logged in!")
            # Test API access
            print("\n📚 Testing course access...")
            courses_found = await page.query_selector_all('a[href*="/courses/"]')
            if courses_found:
                print(f"✅ Can see {len(courses_found)} course links")
                # Save updated session
                await context.storage_state(path=str(SESSION_FILE))
                print(f"💾 Session saved to {SESSION_FILE.name}")
                await browser.close()
                await playwright.stop()
                print("\n✅ Session is valid! You can now run download_to_gdrive.py")
                return True
    
    print("\n❌ Not logged in or session expired")
    print("\n" + "-"*60)
    print("Please log in to Canvas in the browser window.")
    print("The session will be saved automatically after login.")
    print("-"*60)
    
    # Poll for login success
    max_wait = 120  # 2 minutes
    for i in range(max_wait):
        await asyncio.sleep(1)
        current_url = page.url
        
        # Check if we're now on a Canvas page (not login)
        if "canvas.santarosa.edu" in current_url and "login" not in current_url.lower():
            # Check for dashboard or course content
            dashboard = await page.query_selector('.ic-DashboardCard, #dashboard, table.course-list-table, .course-list, .ic-app-header')
            if dashboard:
                print(f"\n✅ Login detected!")
                # Save the session
                await context.storage_state(path=str(SESSION_FILE))
                print(f"💾 Session saved to {SESSION_FILE.name}")
                
                await browser.close()
                await playwright.stop()
                
                print("\n✅ Done! Session is now valid.")
                return True
        
        if i % 10 == 0 and i > 0:
            print(f"   Waiting for login... ({max_wait - i}s remaining)")
    
    print("\n⏰ Timeout waiting for login")
    await browser.close()
    await playwright.stop()
    return False

if __name__ == "__main__":
    asyncio.run(main())
