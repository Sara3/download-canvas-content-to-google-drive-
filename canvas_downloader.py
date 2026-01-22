"""
Canvas Content Downloader for SRJC
Downloads all course content: PDFs, recordings, files, pages, modules

Usage:
    pip install playwright httpx
    playwright install chromium
    python canvas_downloader.py

Set environment variables:
    CANVAS_STUDENT_ID=your_student_id
    CANVAS_PIN=your_pin
    DOWNLOAD_DIR=./canvas_downloads  (optional)
"""

import os
import re
import json
import asyncio
from pathlib import Path
from datetime import datetime
from urllib.parse import urljoin, urlparse, unquote
from playwright.async_api import async_playwright, Page, Browser
import httpx
from dotenv import load_dotenv

# Load environment variables from .env file
load_dotenv()

# Configuration
CANVAS_URL = "https://canvas.santarosa.edu"
STUDENT_ID = os.getenv("CANVAS_STUDENT_ID", "")
PIN = os.getenv("CANVAS_PIN", "")

# Google Drive path on Mac (adjust if needed)
def get_default_download_dir():
    """Find Google Drive folder on Mac."""
    home = Path.home()
    
    # Try common Google Drive locations on Mac
    possible_paths = [
        home / "Google Drive" / "My Drive" / "Canvas",
        home / "Library" / "CloudStorage" / "GoogleDrive-*" / "My Drive" / "Canvas",
        home / "GoogleDrive" / "My Drive" / "Canvas",
    ]
    
    # Check for CloudStorage pattern (newer Google Drive)
    cloud_storage = home / "Library" / "CloudStorage"
    if cloud_storage.exists():
        for folder in cloud_storage.iterdir():
            if folder.name.startswith("GoogleDrive"):
                gdrive_path = folder / "My Drive" / "Canvas"
                gdrive_path.mkdir(parents=True, exist_ok=True)
                return gdrive_path
    
    # Fall back to simple path
    for p in possible_paths:
        if "*" not in str(p) and p.parent.exists():
            p.mkdir(parents=True, exist_ok=True)
            return p
    
    # Last resort: local folder
    local = Path("./canvas_downloads")
    local.mkdir(parents=True, exist_ok=True)
    return local

DOWNLOAD_DIR = Path(os.getenv("DOWNLOAD_DIR", "")) or get_default_download_dir()

# File extensions to download
DOWNLOADABLE_EXTENSIONS = {
    '.pdf', '.doc', '.docx', '.ppt', '.pptx', '.xls', '.xlsx',
    '.zip', '.txt', '.csv', '.mp4', '.mp3', '.m4a', '.mov', '.avi',
    '.png', '.jpg', '.jpeg', '.gif', '.webp', '.svg'
}


class CanvasDownloader:
    def __init__(self, headless: bool = True):
        self.headless = headless
        self.browser: Browser = None
        self.page: Page = None
        self.context = None
        self.playwright = None
        self.downloaded_files: set = set()
        self.download_log: list = []
        # NEW: Structured content tracking for agent ingestion
        self.content_manifest: list = []
        self.quiz_questions: list = []
        self.current_course: dict = None
        # Session persistence
        self.session_file = Path(__file__).parent / ".canvas_session.json"
        
    async def start(self):
        """Start browser with persistent session."""
        self.playwright = await async_playwright().start()
        
        # Use more realistic browser settings to avoid detection
        self.browser = await self.playwright.chromium.launch(
            headless=self.headless,
            args=['--disable-blink-features=AutomationControlled']
        )
        
        # Load existing session if available
        storage_state = None
        if self.session_file.exists():
            try:
                storage_state = str(self.session_file)
                print(f"📂 Loading saved session from {self.session_file.name}")
            except Exception:
                storage_state = None
        
        context = await self.browser.new_context(
            user_agent='Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
            viewport={'width': 1280, 'height': 720},
            storage_state=storage_state
        )
        self.context = context
        self.page = await context.new_page()
        
        # Set longer timeout for slow pages
        self.page.set_default_timeout(60000)
        
    async def stop(self):
        """Stop browser and save session."""
        if self.context:
            await self.save_session()
        if self.browser:
            await self.browser.close()
        if self.playwright:
            await self.playwright.stop()
            
    async def login(self) -> bool:
        """Login to Canvas using Student ID and PIN."""
        print(f"🔐 Logging into Canvas...")
        
        await self.page.goto(CANVAS_URL)
        await self.page.wait_for_load_state("networkidle")
        await asyncio.sleep(3)
        
        # Check if already logged in (dashboard visible)
        current_url = self.page.url
        print(f"   Current URL: {current_url}")
        
        if "canvas.santarosa.edu" in current_url and "login" not in current_url.lower():
            dashboard = await self.page.query_selector('.ic-DashboardCard, #dashboard, .dashboard-header, h1:has-text("Dashboard")')
            if dashboard:
                print("✅ Already logged in!")
                await self.save_session()
                return True
        
        # SRJC uses a portal login with specific field placeholders
        try:
            # Wait for the SRJC login form (might not appear if already logged in)
            await self.page.wait_for_selector('input[placeholder*="username"], input[placeholder*="Username"], input[name="username"], #username', timeout=15000)
            
            # Fill username using placeholder selector (visible field)
            username_field = await self.page.query_selector('input[placeholder*="username"], input[placeholder*="Username"]')
            if username_field:
                await username_field.fill(STUDENT_ID)
            
            # Fill password using placeholder selector (visible field)
            password_field = await self.page.query_selector('input[placeholder*="password"], input[placeholder*="Password"]')
            if password_field:
                await password_field.fill(PIN)
            
            # Click the Login button
            login_btn = await self.page.query_selector('button:has-text("Login"), input[value="Login"], button[type="submit"]')
            if login_btn:
                await login_btn.click()
            
            # Wait for page to load after login
            await asyncio.sleep(5)
            
            # Check if we're on Canvas (dashboard could be at root or /dashboard)
            current_url = self.page.url
            print(f"   Current URL: {current_url}")
            
            # Look for dashboard elements instead of URL
            dashboard = await self.page.query_selector('.ic-DashboardCard, #dashboard, .dashboard-header, h1:has-text("Dashboard")')
            if dashboard or "canvas.santarosa.edu" in current_url:
                # Close any welcome popup
                close_btn = await self.page.query_selector('button:has-text("Close")')
                if close_btn:
                    await close_btn.click()
                    await asyncio.sleep(1)
                print("✅ Logged in successfully!")
                # Save session for future runs
                await self.save_session()
                return True
            else:
                raise Exception("Dashboard not found")
            
        except Exception as e:
            print(f"❌ Login failed: {e}")
            # Save screenshot for debugging (only if page still open)
            try:
                await self.page.screenshot(path="login_error.png")
                print("📸 Screenshot saved to login_error.png")
            except:
                print("   (Could not save screenshot - browser may have closed)")
            return False
    
    async def save_session(self):
        """Save browser session (cookies, localStorage) for future runs."""
        try:
            await self.context.storage_state(path=str(self.session_file))
            print(f"💾 Session saved to {self.session_file.name}")
        except Exception as e:
            print(f"⚠️ Could not save session: {e}")
    
    async def check_existing_session(self) -> bool:
        """Check if we're already logged in from a saved session."""
        if not self.session_file.exists():
            return False
        
        try:
            print("🔍 Checking saved session...")
            await self.page.goto(f"{CANVAS_URL}/courses", timeout=30000)
            await self.page.wait_for_load_state("domcontentloaded")
            await asyncio.sleep(2)
            
            # Check if we're on Canvas (not redirected to login)
            current_url = self.page.url
            if "canvas.santarosa.edu" in current_url and "login" not in current_url.lower():
                dashboard = await self.page.query_selector('.ic-DashboardCard, #dashboard, .dashboard-header, table.course-list-table')
                if dashboard:
                    print("✅ Already logged in from saved session!")
                    return True
            
            print("⚠️ Saved session expired, need to login again")
            # Delete expired session
            self.session_file.unlink(missing_ok=True)
            return False
        except Exception as e:
            print(f"⚠️ Session check failed: {e}")
            return False
    
    async def get_courses(self) -> list:
        """Get list of enrolled courses."""
        print("📚 Fetching courses...")
        
        await self.page.goto(f"{CANVAS_URL}/courses", timeout=120000)
        await self.page.wait_for_load_state("domcontentloaded")
        
        courses = []
        
        # Find course links
        course_links = await self.page.query_selector_all('a[href*="/courses/"][class*="course"], tr.course a, a.ic-DashboardCard__link')
        
        # If dashboard cards didn't work, try the courses page table
        if not course_links:
            course_links = await self.page.query_selector_all('table a[href*="/courses/"]')
        
        # Also try getting from the sidebar/nav
        if not course_links:
            course_links = await self.page.query_selector_all('a[href*="/courses/"]')
        
        seen_ids = set()
        for link in course_links:
            href = await link.get_attribute("href")
            if href and "/courses/" in href:
                # Extract course ID
                match = re.search(r'/courses/(\d+)', href)
                if match:
                    course_id = match.group(1)
                    if course_id not in seen_ids:
                        seen_ids.add(course_id)
                        name = await link.inner_text()
                        name = name.strip() or f"Course {course_id}"
                        courses.append({
                            "id": course_id,
                            "name": self.sanitize_filename(name),
                            "url": f"{CANVAS_URL}/courses/{course_id}"
                        })
        
        print(f"📚 Found {len(courses)} courses")
        for c in courses:
            print(f"   - {c['name']}")
            
        return courses
    
    async def download_course(self, course: dict):
        """Download all content from a course."""
        course_dir = DOWNLOAD_DIR / course["name"]
        course_dir.mkdir(parents=True, exist_ok=True)
        
        print(f"\n{'='*60}")
        print(f"📖 Downloading: {course['name']}")
        print(f"{'='*60}")
        
        # Reset per-course tracking
        self.current_course = course
        self.content_manifest = []
        self.quiz_questions = []
        
        # Download different content types
        await self.download_files(course, course_dir)
        await self.download_modules(course, course_dir)
        await self.download_pages(course, course_dir)
        await self.download_syllabus(course, course_dir)
        await self.download_announcements(course, course_dir)
        await self.download_quizzes(course, course_dir)  # NEW: Quiz extraction
        
        # Save legacy metadata (for backwards compatibility)
        metadata = {
            "course": course,
            "downloaded_at": datetime.now().isoformat(),
            "files": self.download_log
        }
        with open(course_dir / "_metadata.json", "w") as f:
            json.dump(metadata, f, indent=2)
        
        # NEW: Save structured manifest for agent ingestion
        manifest = {
            "version": "2.0",
            "course": {
                "id": course["id"],
                "name": course["name"],
                "url": course["url"]
            },
            "synced_at": datetime.now().isoformat(),
            "content_items": self.content_manifest,
            "quiz_questions": self.quiz_questions,
            "stats": {
                "total_items": len(self.content_manifest),
                "total_quizzes": len(set(q.get("quiz_id") for q in self.quiz_questions)),
                "total_questions": len(self.quiz_questions),
                "files_downloaded": len(self.downloaded_files)
            }
        }
        with open(course_dir / "_manifest.json", "w") as f:
            json.dump(manifest, f, indent=2)
        
        print(f"   📋 Manifest saved: {len(self.content_manifest)} items, {len(self.quiz_questions)} quiz questions")
    
    async def download_files(self, course: dict, course_dir: Path):
        """Download all files from the Files section, including nested folders."""
        files_dir = course_dir / "files"
        files_dir.mkdir(exist_ok=True)
        
        print("📁 Downloading files...")
        
        try:
            await self.download_files_recursive(f"{course['url']}/files", files_dir)
                    
        except Exception as e:
            print(f"   ⚠️ Error downloading files: {e}")
    
    async def download_files_recursive(self, url: str, dest_dir: Path, depth: int = 0):
        """Recursively download files from Canvas file browser."""
        if depth > 5:  # Prevent infinite recursion
            return
            
        indent = "   " + "  " * depth
        
        await self.page.goto(url, timeout=120000)
        await self.page.wait_for_load_state("domcontentloaded")
        await asyncio.sleep(3)
        
        # Find all items (files and folders)
        rows = await self.page.query_selector_all('tr.ef-item-row, .ef-item-row')
        
        if not rows:
            print(f"{indent}No items found")
            return
        
        print(f"{indent}Found {len(rows)} items")
        
        # Collect folder and file URLs first (to avoid navigation issues)
        items = []
        for row in rows:
            try:
                name_link = await row.query_selector('a.ef-name-col__link')
                
                if not name_link:
                    continue
                    
                href = await name_link.get_attribute("href")
                name = await name_link.inner_text()
                name = name.strip() if name else "untitled"
                
                if not href:
                    continue
                
                # Make absolute URL
                if href.startswith("/"):
                    href = f"{CANVAS_URL}{href}"
                
                # Canvas folders have URL pattern: /files/folder/FOLDERNAME
                # Canvas files have URL pattern: /files/FILEID (numeric)
                is_folder = "/files/folder/" in href
                
                items.append({"name": name, "href": href, "is_folder": is_folder})
                    
            except Exception as e:
                print(f"{indent}⚠️ Error reading item: {e}")
        
        # Now process items
        for item in items:
            name = item["name"]
            href = item["href"]
            
            if item["is_folder"]:
                # It's a folder - recurse into it
                print(f"{indent}📂 {name}/")
                folder_dir = dest_dir / self.sanitize_filename(name)
                folder_dir.mkdir(exist_ok=True)
                await self.download_files_recursive(href, folder_dir, depth + 1)
            else:
                # It's a file - download it
                # Extract file ID and create download URL
                download_url = href.rstrip("/") + "/download"
                
                print(f"{indent}📄 {name}")
                await self.download_file(download_url, dest_dir, name)
    
    async def download_modules(self, course: dict, course_dir: Path):
        """Download content from all modules."""
        modules_dir = course_dir / "modules"
        modules_dir.mkdir(exist_ok=True)
        
        print("📦 Downloading modules...")
        
        try:
            await self.page.goto(f"{course['url']}/modules", timeout=120000)
            await self.page.wait_for_load_state("domcontentloaded")
            await asyncio.sleep(3)
            
            # Save screenshot for debugging
            await self.page.screenshot(path=str(course_dir / "modules_page.png"))
            
            # Expand all modules first - try multiple approaches
            expand_selectors = [
                'button[aria-expanded="false"]',
                '.expand_module_link',
                '.ig-header button',
                '[data-module-item-id] button'
            ]
            
            for sel in expand_selectors:
                expand_buttons = await self.page.query_selector_all(sel)
                for btn in expand_buttons:
                    try:
                        await btn.click()
                        await asyncio.sleep(0.3)
                    except:
                        pass
            
            await asyncio.sleep(2)
            
            # Find all modules using multiple selectors
            module_selectors = ['.context_module', 'div[data-module-id]', '.module']
            modules = []
            for sel in module_selectors:
                modules = await self.page.query_selector_all(sel)
                if modules:
                    print(f"   Found {len(modules)} modules using selector: {sel}")
                    break
            
            if not modules:
                print("   No modules found - trying to find all content links directly")
                # Fall back to finding all links on the page
                all_links = await self.page.query_selector_all('a[href*="/files/"], a[href*="/pages/"]')
                print(f"   Found {len(all_links)} content links")
                for link in all_links:
                    href = await link.get_attribute("href")
                    name = await link.inner_text()
                    if href and name:
                        if "/files/" in href:
                            if "/download" not in href:
                                href = href.rstrip("/") + "/download"
                            await self.download_file(href, modules_dir, name.strip())
                        elif "/pages/" in href:
                            await self.save_page_content(href, modules_dir, name.strip())
                return
            
            for i, module in enumerate(modules):
                # Get module name - try multiple selectors
                header_selectors = ['.ig-header-title', '.name', 'h2', '.header-title', 'span.name']
                module_name = f"Module_{i+1}"
                for sel in header_selectors:
                    header = await module.query_selector(sel)
                    if header:
                        text = await header.inner_text()
                        if text.strip():
                            module_name = self.sanitize_filename(text.strip())
                            break
                
                module_dir = modules_dir / module_name
                module_dir.mkdir(exist_ok=True)
                
                print(f"   📦 {module_name}")
                
                # Get all items in module - try multiple selectors
                item_selectors = ['.ig-row', '.context_module_item', 'li.context_module_item', '.module_item']
                items = []
                for sel in item_selectors:
                    items = await module.query_selector_all(sel)
                    if items:
                        break
                
                print(f"      Found {len(items)} items")
                
                for item in items:
                    await self.download_module_item(item, module_dir, course)
                    
        except Exception as e:
            print(f"   ⚠️ Error downloading modules: {e}")
    
    async def download_module_item(self, item, module_dir: Path, course: dict):
        """Download a single module item."""
        try:
            # Get the link - try multiple selectors
            link_selectors = ['a.ig-title', 'a.title', 'a[class*="item"]', 'a[href]', '.item_link a']
            link = None
            for sel in link_selectors:
                link = await item.query_selector(sel)
                if link:
                    break
            
            if not link:
                return
                
            href = await link.get_attribute("href")
            title = await link.inner_text()
            title = self.sanitize_filename(title.strip()) if title else "untitled"
            
            if not href:
                return
            
            # Skip empty or javascript links
            if href == "#" or href.startswith("javascript:"):
                return
                
            # Make absolute URL
            if href.startswith("/"):
                href = f"{CANVAS_URL}{href}"
            
            print(f"      → {title[:50]}...")
            
            # Check item type
            if "/files/" in href:
                # File - ensure download URL
                download_href = href.rstrip("/")
                if "/download" not in download_href and "verifier=" not in download_href:
                    download_href += "/download"
                await self.download_file(download_href, module_dir, title)
                
            elif "/pages/" in href:
                # Wiki page - save as HTML
                await self.save_page_content(href, module_dir, title)
                
            elif "/assignments/" in href:
                # Assignment page - save as HTML
                await self.save_page_content(href, module_dir, f"Assignment - {title}")
                
            elif "/quizzes/" in href:
                # Quiz - just note it exists
                print(f"         (Quiz - cannot download)")
                
            elif "/external_tools/" in href or "external_url" in href:
                # External link - might be video
                await self.handle_external_content(href, module_dir, title)
                
            elif any(ext in href.lower() for ext in ['.pdf', '.doc', '.ppt', '.mp4', '.mp3']):
                await self.download_file(href, module_dir, title)
                
        except Exception as e:
            print(f"      ⚠️ Error: {e}")
    
    async def download_file(self, url: str, dest_dir: Path, filename: str = None):
        """Download a file."""
        if url in self.downloaded_files:
            return
            
        try:
            # Make absolute URL
            if url.startswith("/"):
                url = f"{CANVAS_URL}{url}"
            
            # Skip if not a file URL
            if "/files/" not in url and not any(ext in url.lower() for ext in ['.pdf', '.doc', '.ppt', '.mp4', '.mp3', '.zip']):
                return
            
            # Get cookies from browser for authenticated download
            cookies = await self.page.context.cookies()
            cookie_header = "; ".join([f"{c['name']}={c['value']}" for c in cookies])
            
            headers = {
                "Cookie": cookie_header,
                "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"
            }
            
            async with httpx.AsyncClient(follow_redirects=True, timeout=300) as client:
                response = await client.get(url, headers=headers)
                
                if response.status_code == 200:
                    content_type = response.headers.get("content-type", "")
                    
                    # Skip HTML responses (folder pages, not actual files)
                    if "text/html" in content_type and len(response.content) < 50000:
                        # This might be a folder page, not a file
                        return
                    
                    # Determine filename from Content-Disposition header first
                    cd = response.headers.get("content-disposition", "")
                    if "filename=" in cd:
                        match = re.search(r'filename\*?=["\']?(?:UTF-8\'\')?([^";\n\r\']+)["\']?', cd, re.IGNORECASE)
                        if match:
                            filename = unquote(match.group(1))
                    
                    # Fall back to provided filename or URL
                    if not filename:
                        filename = unquote(urlparse(url).path.split("/")[-1])
                    
                    # Clean up filename
                    if filename in ["download", ""] or not filename:
                        filename = f"file_{len(self.downloaded_files)}"
                    
                    filename = self.sanitize_filename(filename)
                    
                    # Add extension based on content type if missing
                    if not Path(filename).suffix or Path(filename).suffix == ".":
                        ext_map = {
                            "application/pdf": ".pdf",
                            "video/mp4": ".mp4",
                            "audio/mpeg": ".mp3",
                            "image/jpeg": ".jpg",
                            "image/png": ".png",
                            "application/msword": ".doc",
                            "application/vnd.openxmlformats-officedocument.wordprocessingml.document": ".docx",
                            "application/vnd.ms-powerpoint": ".ppt",
                            "application/vnd.openxmlformats-officedocument.presentationml.presentation": ".pptx",
                        }
                        for mime, ext in ext_map.items():
                            if mime in content_type:
                                filename = filename.rstrip(".") + ext
                                break
                    
                    # Save file
                    filepath = dest_dir / filename
                    
                    # Handle duplicates
                    counter = 1
                    orig_filename = filename
                    while filepath.exists():
                        stem = Path(orig_filename).stem
                        suffix = Path(orig_filename).suffix
                        filename = f"{stem}_{counter}{suffix}"
                        filepath = dest_dir / filename
                        counter += 1
                    
                    with open(filepath, "wb") as f:
                        f.write(response.content)
                    
                    file_size = len(response.content)
                    self.downloaded_files.add(url)
                    self.download_log.append({
                        "url": url,
                        "path": str(filepath),
                        "size": file_size
                    })
                    
                    # NEW: Add to manifest
                    file_ext = Path(filename).suffix.lower()
                    self.add_to_manifest(
                        content_type="file",
                        title=filename,
                        file_path=str(filepath),
                        source_url=url,
                        file_type=file_ext.lstrip('.'),
                        file_size=file_size
                    )
                    
                    size_str = f"{file_size // 1024} KB" if file_size > 1024 else f"{file_size} B"
                    print(f"         ✅ {filename} ({size_str})")
                elif response.status_code == 404:
                    pass  # File not found, skip silently
                else:
                    print(f"         ⚠️ HTTP {response.status_code} for {filename or url}")
                    
        except Exception as e:
            print(f"         ❌ Failed: {e}")
    
    async def save_page_content(self, url: str, dest_dir: Path, title: str):
        """Save a Canvas page as HTML and extract media."""
        try:
            # Make absolute URL
            if url.startswith("/"):
                url = f"{CANVAS_URL}{url}"
                
            await self.page.goto(url, timeout=60000)
            await self.page.wait_for_load_state("domcontentloaded")
            await asyncio.sleep(3)  # Let content load
            
            # Get page content - try multiple selectors
            content_selectors = [
                '.show-content',
                '#wiki_page_show', 
                '.user_content',
                '#content',
                '.assignment-description',
                'article',
                'main'
            ]
            
            content = None
            for sel in content_selectors:
                content = await self.page.query_selector(sel)
                if content:
                    break
            
            if content:
                html = await content.inner_html()
                
                # Create a complete HTML document with styling
                full_html = f"""<!DOCTYPE html>
<html>
<head>
    <meta charset="UTF-8">
    <title>{title}</title>
    <style>
        body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; 
               max-width: 900px; margin: 40px auto; padding: 20px; line-height: 1.6; }}
        img {{ max-width: 100%; height: auto; }}
        table {{ border-collapse: collapse; width: 100%; }}
        td, th {{ border: 1px solid #ddd; padding: 8px; }}
        a {{ color: #0066cc; }}
        h1, h2, h3 {{ color: #333; }}
    </style>
</head>
<body>
<h1>{title}</h1>
{html}
</body>
</html>"""
                
                # Save HTML
                filename = self.sanitize_filename(title) + ".html"
                filepath = dest_dir / filename
                with open(filepath, "w", encoding="utf-8") as f:
                    f.write(full_html)
                print(f"      ✅ {filename} ({len(full_html) // 1024} KB)")
                
                # NEW: Extract plain text and add to manifest
                plain_text = await content.inner_text()
                self.add_to_manifest(
                    content_type="page",
                    title=title,
                    file_path=str(filepath),
                    source_url=url,
                    file_type="html",
                    text_content=plain_text.strip()[:5000] if plain_text else None,  # Limit text size
                    file_size=len(full_html)
                )
                
                # Download all linked files (PDFs, PPTs, etc.)
                file_links = await content.query_selector_all('a[href*="/files/"]')
                for link in file_links:
                    href = await link.get_attribute("href")
                    link_text = await link.inner_text()
                    if href and href.startswith("/"):
                        href = f"{CANVAS_URL}{href}"
                    if href:
                        # Ensure it's a download link
                        if "/download" not in href and "verifier=" not in href:
                            href = href.rstrip("/") + "/download"
                        name = link_text.strip() if link_text and link_text.strip() not in ["here", "click here", "link"] else None
                        await self.download_file(href, dest_dir, name)
                
                # Extract embedded videos
                await self.extract_videos_from_page(content, dest_dir, title)
            else:
                print(f"      ⚠️ No content found on page")
                        
        except Exception as e:
            print(f"      ⚠️ Error saving page: {e}")
    
    async def extract_videos_from_page(self, content, dest_dir: Path, title: str):
        """Extract and download videos from a page."""
        try:
            # Method 1: Direct video tags
            videos = await content.query_selector_all('video source, video[src]')
            for i, video in enumerate(videos):
                src = await video.get_attribute("src")
                if src:
                    video_title = f"{title}_video_{i+1}" if i > 0 else f"{title}_video"
                    await self.download_file(src, dest_dir, video_title + ".mp4")
            
            # Method 2: Canvas media embeds (iframes)
            iframes = await content.query_selector_all('iframe[src*="media"], iframe[src*="video"], iframe[data-media-id]')
            for i, iframe in enumerate(iframes):
                src = await iframe.get_attribute("src")
                media_id = await iframe.get_attribute("data-media-id")
                
                if src:
                    # Try to get the actual video URL
                    await self.download_video_from_embed(src, dest_dir, f"{title}_lecture_{i+1}")
                elif media_id:
                    # Canvas media object
                    video_url = f"{CANVAS_URL}/media_objects/{media_id}/download"
                    await self.download_file(video_url, dest_dir, f"{title}_lecture_{i+1}.mp4")
            
            # Method 3: Look for media_object links
            media_links = await content.query_selector_all('a[href*="media_objects"], a[href*="/media/"]')
            for i, link in enumerate(media_links):
                href = await link.get_attribute("href")
                if href and "download" not in href:
                    href = href.rstrip("/") + "/download"
                await self.download_file(href, dest_dir, f"{title}_media_{i+1}.mp4")
            
            # Method 4: Check for video player divs with data attributes
            video_containers = await self.page.query_selector_all('[data-media-id], .video_player, .mejs-container')
            for i, container in enumerate(video_containers):
                media_id = await container.get_attribute("data-media-id")
                if media_id:
                    video_url = f"{CANVAS_URL}/media_objects/{media_id}/download"
                    await self.download_file(video_url, dest_dir, f"{title}_video_{i+1}.mp4")
                    
        except Exception as e:
            print(f"      ⚠️ Error extracting videos: {e}")
    
    async def download_video_from_embed(self, embed_url: str, dest_dir: Path, title: str):
        """Navigate to embed and extract actual video source."""
        try:
            # Open embed in new context to not lose current page
            new_page = await self.browser.new_page()
            await new_page.goto(embed_url)
            await new_page.wait_for_load_state("networkidle")
            await asyncio.sleep(3)  # Let video player initialize
            
            # Try to find video source
            video = await new_page.query_selector('video source, video[src]')
            if video:
                src = await video.get_attribute("src")
                if src:
                    await self.download_file(src, dest_dir, title + ".mp4")
            
            await new_page.close()
        except Exception as e:
            print(f"      ⚠️ Error with video embed: {e}")
    
    async def handle_external_content(self, url: str, dest_dir: Path, title: str):
        """Handle external tools/links - often video embeds."""
        try:
            await self.page.goto(url)
            await self.page.wait_for_load_state("networkidle")
            await asyncio.sleep(3)
            
            # Look for video sources
            video_sources = await self.page.query_selector_all('video source, video[src], iframe[src*="youtube"], iframe[src*="vimeo"], iframe[src*="kaltura"], iframe[src*="panopto"], a[href*=".mp4"]')
            
            for source in video_sources:
                src = await source.get_attribute("src") or await source.get_attribute("href")
                if src:
                    # For YouTube/Vimeo, save the link
                    if "youtube" in src or "vimeo" in src:
                        with open(dest_dir / f"{self.sanitize_filename(title)}_video_link.txt", "w") as f:
                            f.write(src)
                        print(f"      📺 Video link saved: {title}")
                    else:
                        await self.download_file(src, dest_dir, title)
                        
        except Exception as e:
            print(f"      ⚠️ Error with external content: {e}")
    
    async def download_pages(self, course: dict, course_dir: Path):
        """Download all wiki pages."""
        pages_dir = course_dir / "pages"
        pages_dir.mkdir(exist_ok=True)
        
        print("📄 Downloading pages...")
        
        try:
            await self.page.goto(f"{course['url']}/pages")
            await self.page.wait_for_load_state("networkidle")
            
            page_links = await self.page.query_selector_all('a.wiki-page-link, a[href*="/pages/"]')
            
            for link in page_links:
                href = await link.get_attribute("href")
                title = await link.inner_text()
                
                if href and "/pages/" in href and "/edit" not in href:
                    if href.startswith("/"):
                        href = f"{CANVAS_URL}{href}"
                    await self.save_page_content(href, pages_dir, title.strip())
                    
        except Exception as e:
            print(f"   ⚠️ Error downloading pages: {e}")
    
    async def download_syllabus(self, course: dict, course_dir: Path):
        """Download syllabus."""
        print("📋 Downloading syllabus...")
        
        try:
            await self.page.goto(f"{course['url']}/assignments/syllabus")
            await self.page.wait_for_load_state("networkidle")
            
            content = await self.page.query_selector('#course_syllabus, .syllabus, .user_content')
            if content:
                html = await content.inner_html()
                plain_text = await content.inner_text()
                filepath = course_dir / "syllabus.html"
                with open(filepath, "w", encoding="utf-8") as f:
                    f.write(f"<html><head><title>Syllabus</title></head><body>{html}</body></html>")
                print("      ✅ syllabus.html")
                
                # NEW: Add to manifest
                self.add_to_manifest(
                    content_type="syllabus",
                    title="Course Syllabus",
                    file_path=str(filepath),
                    source_url=f"{course['url']}/assignments/syllabus",
                    file_type="html",
                    text_content=plain_text.strip()[:5000] if plain_text else None
                )
                
        except Exception as e:
            print(f"   ⚠️ Error downloading syllabus: {e}")
    
    async def download_announcements(self, course: dict, course_dir: Path):
        """Download announcements."""
        print("📢 Downloading announcements...")
        
        try:
            await self.page.goto(f"{course['url']}/announcements")
            await self.page.wait_for_load_state("networkidle")
            
            announcements = []
            items = await self.page.query_selector_all('.ic-announcement-row, .discussion-topic, tr.discussion-topic')
            
            for item in items:
                title_el = await item.query_selector('a.ic-announcement-row__content, a.discussion-title, a.title')
                if title_el:
                    title = await title_el.inner_text()
                    href = await title_el.get_attribute("href")
                    announcements.append({"title": title.strip(), "url": href})
            
            if announcements:
                filepath = course_dir / "announcements.json"
                with open(filepath, "w") as f:
                    json.dump(announcements, f, indent=2)
                print(f"      ✅ {len(announcements)} announcements saved")
                
                # NEW: Add each announcement to manifest
                for ann in announcements:
                    self.add_to_manifest(
                        content_type="announcement",
                        title=ann["title"],
                        source_url=ann.get("url"),
                        file_path=str(filepath)
                    )
                
        except Exception as e:
            print(f"   ⚠️ Error downloading announcements: {e}")
    
    def add_to_manifest(self, content_type: str, title: str, **kwargs):
        """Add an item to the content manifest for agent ingestion."""
        item = {
            "content_type": content_type,
            "title": title,
            "course_id": self.current_course["id"] if self.current_course else None,
            "course_name": self.current_course["name"] if self.current_course else None,
            "synced_at": datetime.now().isoformat(),
            **kwargs
        }
        self.content_manifest.append(item)
    
    async def download_quizzes(self, course: dict, course_dir: Path):
        """Download quiz information and questions (from completed quizzes)."""
        quizzes_dir = course_dir / "quizzes"
        quizzes_dir.mkdir(exist_ok=True)
        
        print("📝 Downloading quizzes...")
        
        try:
            await self.page.goto(f"{course['url']}/quizzes")
            await self.page.wait_for_load_state("networkidle")
            await asyncio.sleep(2)
            
            # Find all quiz links
            quiz_links = await self.page.query_selector_all('a.ig-title, a[href*="/quizzes/"]')
            
            quizzes = []
            seen_ids = set()
            
            for link in quiz_links:
                href = await link.get_attribute("href")
                title = await link.inner_text()
                
                if href and "/quizzes/" in href:
                    # Extract quiz ID
                    match = re.search(r'/quizzes/(\d+)', href)
                    if match and match.group(1) not in seen_ids:
                        quiz_id = match.group(1)
                        seen_ids.add(quiz_id)
                        quizzes.append({
                            "id": quiz_id,
                            "title": title.strip() if title else f"Quiz {quiz_id}",
                            "url": href if href.startswith("http") else f"{CANVAS_URL}{href}"
                        })
            
            print(f"   Found {len(quizzes)} quizzes")
            
            # Try to get quiz details and questions
            for quiz in quizzes:
                await self.extract_quiz_content(quiz, quizzes_dir, course)
            
            # Save quiz summary
            if quizzes:
                with open(quizzes_dir / "_quiz_list.json", "w") as f:
                    json.dump(quizzes, f, indent=2)
                
        except Exception as e:
            print(f"   ⚠️ Error downloading quizzes: {e}")
    
    async def extract_quiz_content(self, quiz: dict, quizzes_dir: Path, course: dict):
        """Extract questions from a quiz (if viewable in review mode)."""
        try:
            # First try to get quiz details page
            await self.page.goto(quiz["url"])
            await self.page.wait_for_load_state("networkidle")
            await asyncio.sleep(2)
            
            quiz_data = {
                "quiz_id": quiz["id"],
                "title": quiz["title"],
                "questions": []
            }
            
            # Check for due date
            due_date_el = await self.page.query_selector('.quiz-due-date, .due_date_display, .date-due')
            if due_date_el:
                quiz_data["due_date"] = await due_date_el.inner_text()
            
            # Check for points possible
            points_el = await self.page.query_selector('.points_possible, .quiz-points-possible')
            if points_el:
                quiz_data["points_possible"] = await points_el.inner_text()
            
            # Try to access quiz review (for completed quizzes)
            # Look for "View Results" or similar link
            review_link = await self.page.query_selector('a:has-text("View"), a:has-text("Results"), a:has-text("Review")')
            
            if review_link:
                review_href = await review_link.get_attribute("href")
                if review_href:
                    if review_href.startswith("/"):
                        review_href = f"{CANVAS_URL}{review_href}"
                    
                    await self.page.goto(review_href)
                    await self.page.wait_for_load_state("networkidle")
                    await asyncio.sleep(2)
                    
                    # Extract questions from quiz review
                    questions = await self.page.query_selector_all('.question, .quiz_question, .display_question')
                    
                    for i, q in enumerate(questions):
                        question_data = await self.extract_question(q, i + 1)
                        if question_data:
                            question_data["quiz_id"] = quiz["id"]
                            question_data["quiz_title"] = quiz["title"]
                            question_data["course_id"] = course["id"]
                            question_data["course_name"] = course["name"]
                            quiz_data["questions"].append(question_data)
                            self.quiz_questions.append(question_data)
            
            # Save quiz data
            if quiz_data["questions"]:
                filename = self.sanitize_filename(quiz["title"]) + ".json"
                with open(quizzes_dir / filename, "w") as f:
                    json.dump(quiz_data, f, indent=2)
                print(f"      ✅ {quiz['title']}: {len(quiz_data['questions'])} questions")
                
                # Add to manifest
                self.add_to_manifest(
                    content_type="quiz",
                    title=quiz["title"],
                    quiz_id=quiz["id"],
                    question_count=len(quiz_data["questions"]),
                    due_date=quiz_data.get("due_date"),
                    file_path=str(quizzes_dir / filename)
                )
            else:
                print(f"      ⏳ {quiz['title']}: No questions available (not completed?)")
                
        except Exception as e:
            print(f"      ⚠️ Error extracting quiz {quiz['title']}: {e}")
    
    async def extract_question(self, question_el, question_num: int) -> dict:
        """Extract a single quiz question."""
        try:
            # Get question text
            text_el = await question_el.query_selector('.question_text, .text, p')
            question_text = ""
            if text_el:
                question_text = await text_el.inner_text()
            
            if not question_text.strip():
                return None
            
            question_data = {
                "question_number": question_num,
                "question_text": question_text.strip(),
                "question_type": "unknown",
                "options": [],
                "correct_answer": None
            }
            
            # Check for multiple choice options
            options = await question_el.query_selector_all('.answer, .answer_text, label')
            if options:
                question_data["question_type"] = "multiple_choice"
                for opt in options:
                    opt_text = await opt.inner_text()
                    if opt_text.strip():
                        # Check if this is the correct answer
                        is_correct = await opt.query_selector('.correct_answer, .correct, .selected_answer.correct')
                        question_data["options"].append({
                            "text": opt_text.strip(),
                            "correct": is_correct is not None
                        })
                        if is_correct:
                            question_data["correct_answer"] = opt_text.strip()
            
            # Check for true/false
            if any("true" in str(opt.get("text", "")).lower() for opt in question_data["options"]):
                if len(question_data["options"]) == 2:
                    question_data["question_type"] = "true_false"
            
            # Check for short answer / essay
            text_input = await question_el.query_selector('textarea, input[type="text"]')
            if text_input and not question_data["options"]:
                question_data["question_type"] = "short_answer"
            
            # Try to find correct answer display
            correct_el = await question_el.query_selector('.correct_answer, .answer_text.correct, .selected_answer.correct')
            if correct_el and not question_data["correct_answer"]:
                question_data["correct_answer"] = await correct_el.inner_text()
            
            return question_data
            
        except Exception as e:
            print(f"         ⚠️ Error extracting question: {e}")
            return None
    
    def sanitize_filename(self, name: str) -> str:
        """Make string safe for filename."""
        # Remove/replace invalid characters
        name = re.sub(r'[<>:"/\\|?*]', '_', name)
        name = re.sub(r'\s+', ' ', name)
        name = name.strip('. ')
        return name[:100]  # Limit length


async def main():
    """Main entry point."""
    print("""
╔═══════════════════════════════════════════════════════════╗
║           Canvas Content Downloader for SRJC              ║
╚═══════════════════════════════════════════════════════════╝
    """)
    
    if not STUDENT_ID or not PIN:
        print("❌ Please set environment variables:")
        print("   export CANVAS_STUDENT_ID=your_student_id")
        print("   export CANVAS_PIN=your_pin")
        print("\nOr create a .env file with these values.")
        return
    
    DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)
    print(f"📂 Download directory: {DOWNLOAD_DIR.absolute()}")
    
    # Use headless=True for background/terminal operation, headless=False for debugging
    headless_mode = os.getenv("HEADLESS", "true").lower() == "true"
    downloader = CanvasDownloader(headless=headless_mode)
    
    try:
        await downloader.start()
        
        # Try using existing session first
        session_valid = await downloader.check_existing_session()
        
        if not session_valid:
            # Need to login - if headless mode failed, suggest visible browser
            if not await downloader.login():
                if headless_mode:
                    print("\n" + "="*60)
                    print("💡 TIP: SSO may be blocking headless browsers.")
                    print("   Try running with a visible browser:")
                    print("   HEADLESS=false python canvas_downloader.py")
                    print("="*60)
                return
        
        courses = await downloader.get_courses()
        
        if not courses:
            print("❌ No courses found!")
            return
        
        # Download each course
        for course in courses:
            await downloader.download_course(course)
        
        print(f"\n{'='*60}")
        print("✅ Download complete!")
        print(f"📂 Files saved to: {DOWNLOAD_DIR.absolute()}")
        print(f"📊 Total files: {len(downloader.downloaded_files)}")
        
    finally:
        await downloader.stop()


if __name__ == "__main__":
    asyncio.run(main())
