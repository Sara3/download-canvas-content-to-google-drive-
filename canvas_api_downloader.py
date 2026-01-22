"""
Canvas API Downloader for SRJC
Uses Canvas REST API with session cookies (no API token needed!)

Usage:
    1. First run canvas_downloader.py with HEADLESS=false to login and save session
    2. Then run this script to download all content via API

    python canvas_api_downloader.py
"""

import os
import json
import asyncio
import re
from pathlib import Path
from datetime import datetime
from urllib.parse import urljoin, unquote
import httpx
from dotenv import load_dotenv
from html.parser import HTMLParser

# PDF text extraction
try:
    import fitz  # PyMuPDF
    PDF_SUPPORT = True
except ImportError:
    PDF_SUPPORT = False
    print("⚠️ PyMuPDF not installed - PDF text extraction disabled")
    print("   Install with: pip install pymupdf")


class HTMLToText(HTMLParser):
    """Convert HTML to clean, readable plain text."""
    
    def __init__(self):
        super().__init__()
        self.text_parts = []
        self.links = []
        self.current_link = None
        self.in_script = False
        self.in_style = False
        self.list_depth = 0
        self.in_heading = False
    
    def handle_starttag(self, tag, attrs):
        attrs_dict = dict(attrs)
        
        if tag == 'script':
            self.in_script = True
        elif tag == 'style':
            self.in_style = True
        elif tag == 'a':
            href = attrs_dict.get('href', '')
            if href and not href.startswith('#'):
                self.current_link = href
        elif tag == 'br':
            self.text_parts.append('\n')
        elif tag in ('p', 'div'):
            self.text_parts.append('\n\n')
        elif tag in ('h1', 'h2', 'h3', 'h4', 'h5', 'h6'):
            self.text_parts.append('\n\n')
            self.in_heading = True
        elif tag in ('ul', 'ol'):
            self.text_parts.append('\n')
            self.list_depth += 1
        elif tag == 'li':
            self.text_parts.append('\n' + '  ' * (self.list_depth - 1) + '• ')
        elif tag == 'img':
            alt = attrs_dict.get('alt', '')
            src = attrs_dict.get('src', '')
            if alt:
                self.text_parts.append(f'[Image: {alt}]')
            elif src:
                self.text_parts.append(f'[Image]')
        elif tag == 'iframe':
            src = attrs_dict.get('src', '')
            title = attrs_dict.get('title', 'Embedded content')
            if src:
                self.links.append({'text': f'[Video: {title}]', 'url': src})
                self.text_parts.append(f'\n[Video: {title}]\n')
    
    def handle_endtag(self, tag):
        if tag == 'script':
            self.in_script = False
        elif tag == 'style':
            self.in_style = False
        elif tag == 'a':
            if self.current_link:
                self.links.append({'text': ''.join(self.text_parts[-1:]).strip(), 'url': self.current_link})
            self.current_link = None
        elif tag in ('h1', 'h2', 'h3', 'h4', 'h5', 'h6'):
            self.in_heading = False
            self.text_parts.append('\n')
        elif tag in ('ul', 'ol'):
            self.list_depth = max(0, self.list_depth - 1)
            self.text_parts.append('\n')
    
    def handle_data(self, data):
        if self.in_script or self.in_style:
            return
        
        text = data.strip()
        if text:
            if self.in_heading:
                self.text_parts.append(text.upper())
            else:
                self.text_parts.append(text)
    
    def get_text(self) -> str:
        """Get clean text output."""
        text = ' '.join(self.text_parts)
        # Clean up whitespace
        text = re.sub(r'\n\s*\n\s*\n+', '\n\n', text)  # Max 2 newlines
        text = re.sub(r' +', ' ', text)  # Single spaces
        text = re.sub(r'\n +', '\n', text)  # No leading spaces after newline
        return text.strip()
    
    def get_links(self) -> list:
        """Get list of links found in the HTML."""
        # Deduplicate and filter
        seen = set()
        unique_links = []
        for link in self.links:
            url = link['url']
            if url not in seen and url.startswith('http'):
                seen.add(url)
                unique_links.append(link)
        return unique_links


def html_to_text(html: str) -> tuple[str, list]:
    """Convert HTML to clean text and extract links."""
    parser = HTMLToText()
    try:
        parser.feed(html)
        return parser.get_text(), parser.get_links()
    except:
        # Fallback: simple regex strip
        text = re.sub(r'<script[^>]*>.*?</script>', '', html, flags=re.DOTALL | re.IGNORECASE)
        text = re.sub(r'<style[^>]*>.*?</style>', '', text, flags=re.DOTALL | re.IGNORECASE)
        text = re.sub(r'<[^>]+>', ' ', text)
        text = re.sub(r'\s+', ' ', text).strip()
        return text, []

load_dotenv()

# Configuration
CANVAS_URL = "https://canvas.santarosa.edu"
DOWNLOAD_DIR = Path(os.getenv("DOWNLOAD_DIR", "./canvas_downloads"))
SESSION_FILE = Path(__file__).parent / ".canvas_session.json"


class CanvasAPIDownloader:
    def __init__(self, force_full_sync: bool = False):
        self.cookies = {}
        self.headers = {
            "Accept": "application/json",
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"
        }
        self.downloaded_files = set()
        self.content_manifest = []
        self.force_full_sync = force_full_sync
        self.sync_state_file = DOWNLOAD_DIR / "_sync_state.json"
        self.sync_state = self.load_sync_state()
        self.items_skipped = 0
        self.items_updated = 0
        self.items_new = 0
    
    def load_sync_state(self) -> dict:
        """Load previous sync state to enable incremental updates."""
        if self.sync_state_file.exists():
            try:
                with open(self.sync_state_file) as f:
                    return json.load(f)
            except:
                pass
        return {"courses": {}, "last_sync": None}
    
    def save_sync_state(self):
        """Save sync state for future incremental syncs."""
        self.sync_state["last_sync"] = datetime.now().isoformat()
        with open(self.sync_state_file, "w") as f:
            json.dump(self.sync_state, f, indent=2)
    
    def get_course_state(self, course_id: str) -> dict:
        """Get sync state for a specific course."""
        return self.sync_state["courses"].get(str(course_id), {
            "last_sync": None,
            "items": {}  # item_id -> {"updated_at": ..., "file_path": ...}
        })
    
    def save_course_state(self, course_id: str, state: dict):
        """Save sync state for a specific course."""
        state["last_sync"] = datetime.now().isoformat()
        self.sync_state["courses"][str(course_id)] = state
    
    def needs_update(self, course_state: dict, item_id: str, updated_at: str, file_path: Path = None) -> bool:
        """Check if an item needs to be downloaded (new or changed)."""
        if self.force_full_sync:
            return True
        
        item_state = course_state.get("items", {}).get(str(item_id))
        if not item_state:
            self.items_new += 1
            return True  # New item
        
        # Check if updated since last download
        if item_state.get("updated_at") != updated_at:
            self.items_updated += 1
            return True  # Changed
        
        # Check if file exists
        if file_path and not file_path.exists():
            self.items_new += 1
            return True  # File missing
        
        self.items_skipped += 1
        return False  # Up to date
    
    def mark_downloaded(self, course_state: dict, item_id: str, updated_at: str, file_path: str = None):
        """Mark an item as downloaded in the sync state."""
        if "items" not in course_state:
            course_state["items"] = {}
        course_state["items"][str(item_id)] = {
            "updated_at": updated_at,
            "file_path": file_path,
            "synced_at": datetime.now().isoformat()
        }
        
    def load_session(self) -> bool:
        """Load session cookies from file."""
        if not SESSION_FILE.exists():
            print("❌ No session file found!")
            print("   Run 'HEADLESS=false python canvas_downloader.py' first to login")
            return False
        
        with open(SESSION_FILE) as f:
            data = json.load(f)
        
        self.cookies = {c["name"]: c["value"] for c in data.get("cookies", [])}
        print(f"✅ Loaded session with {len(self.cookies)} cookies")
        return True
    
    async def api_get(self, endpoint: str, params: dict = None) -> dict | list | None:
        """Make authenticated API request."""
        url = f"{CANVAS_URL}/api/v1{endpoint}"
        
        async with httpx.AsyncClient(cookies=self.cookies, headers=self.headers, 
                                      follow_redirects=True, timeout=60) as client:
            try:
                resp = await client.get(url, params=params)
                if resp.status_code == 200:
                    return resp.json()
                else:
                    print(f"   ⚠️ API error {resp.status_code}: {endpoint}")
                    return None
            except Exception as e:
                print(f"   ⚠️ Request error: {e}")
                return None
    
    async def api_get_paginated(self, endpoint: str, params: dict = None) -> list:
        """Get all pages of a paginated API response."""
        results = []
        params = params or {}
        params["per_page"] = 100
        
        async with httpx.AsyncClient(cookies=self.cookies, headers=self.headers,
                                      follow_redirects=True, timeout=60) as client:
            url = f"{CANVAS_URL}/api/v1{endpoint}"
            
            while url:
                try:
                    resp = await client.get(url, params=params)
                    if resp.status_code != 200:
                        break
                    
                    data = resp.json()
                    if isinstance(data, list):
                        results.extend(data)
                    else:
                        results.append(data)
                    
                    # Check for next page
                    links = resp.headers.get("Link", "")
                    next_url = None
                    for link in links.split(","):
                        if 'rel="next"' in link:
                            next_url = link.split(";")[0].strip("<> ")
                            break
                    url = next_url
                    params = {}  # Clear params for subsequent requests (URL has them)
                    
                except Exception as e:
                    print(f"   ⚠️ Pagination error: {e}")
                    break
        
        return results
    
    async def get_courses(self) -> list:
        """Get list of enrolled courses."""
        print("📚 Fetching courses...")
        courses = await self.api_get_paginated("/courses", {
            "enrollment_state": "active",
            "include[]": ["term", "total_students"]
        })
        
        if courses:
            print(f"   Found {len(courses)} courses")
            for c in courses:
                print(f"   - {c.get('name', 'Unknown')}")
        
        return courses or []
    
    async def download_course(self, course: dict):
        """Download all content from a course (incremental sync)."""
        course_id = course["id"]
        course_name = self.sanitize_filename(course.get("name", f"Course_{course_id}"))
        course_dir = DOWNLOAD_DIR / course_name
        course_dir.mkdir(parents=True, exist_ok=True)
        
        print(f"\n{'='*60}")
        print(f"📖 Syncing: {course_name}")
        print(f"{'='*60}")
        
        # Load course sync state for incremental updates
        self.current_course_state = self.get_course_state(course_id)
        last_sync = self.current_course_state.get("last_sync")
        if last_sync and not self.force_full_sync:
            print(f"   📅 Last sync: {last_sync[:16]}")
        
        # Reset counters for this course
        self.items_skipped = 0
        self.items_updated = 0
        self.items_new = 0
        self.content_manifest = []
        
        # Download different content types
        await self.download_modules(course_id, course_dir)
        await self.download_files(course_id, course_dir)
        await self.download_pages(course_id, course_dir)
        await self.download_assignments(course_id, course_dir)
        await self.download_syllabus(course_id, course_dir)
        await self.download_announcements(course_id, course_dir)
        await self.download_quizzes(course_id, course_dir)
        
        # Extract text from all PDFs (for RAG/study)
        await self.extract_all_pdfs(course_dir)
        
        # Save course sync state
        self.save_course_state(str(course_id), self.current_course_state)
        
        # Save manifest
        manifest = {
            "version": "2.0",
            "course": {
                "id": str(course_id),
                "name": course_name,
                "url": f"{CANVAS_URL}/courses/{course_id}"
            },
            "synced_at": datetime.now().isoformat(),
            "content_items": self.content_manifest,
            "stats": {
                "total_items": len(self.content_manifest),
                "files_downloaded": len(self.downloaded_files)
            }
        }
        
        with open(course_dir / "_manifest.json", "w") as f:
            json.dump(manifest, f, indent=2)
        
        # Show sync summary
        if self.items_skipped > 0:
            print(f"   📊 New: {self.items_new} | Updated: {self.items_updated} | Unchanged: {self.items_skipped}")
        else:
            print(f"   📊 Downloaded {len(self.content_manifest)} items")
    
    async def download_modules(self, course_id: int, course_dir: Path):
        """Download all modules and their items."""
        print("📦 Downloading modules...")
        modules_dir = course_dir / "modules"
        modules_dir.mkdir(exist_ok=True)
        
        modules = await self.api_get_paginated(f"/courses/{course_id}/modules", {
            "include[]": ["items", "content_details"]
        })
        
        if not modules:
            print("   No modules found")
            return
        
        print(f"   Found {len(modules)} modules")
        
        for i, module in enumerate(modules):
            module_name = self.sanitize_filename(module.get("name", f"Module_{i}"))
            module_dir = modules_dir / module_name
            module_dir.mkdir(exist_ok=True)
            
            print(f"   📁 {module_name}")
            
            # Get module items
            items = module.get("items", [])
            if not items:
                items = await self.api_get_paginated(
                    f"/courses/{course_id}/modules/{module['id']}/items",
                    {"include[]": ["content_details"]}
                ) or []
            
            for item in items:
                await self.download_module_item(course_id, item, module_dir, module_name)
            
            # Add to manifest
            self.content_manifest.append({
                "content_type": "module",
                "title": module.get("name"),
                "module_id": module.get("id"),
                "position": module.get("position"),
                "items_count": len(items),
                "synced_at": datetime.now().isoformat()
            })
    
    async def download_module_item(self, course_id: int, item: dict, dest_dir: Path, module_name: str):
        """Download a single module item."""
        item_type = item.get("type", "").lower()
        title = self.sanitize_filename(item.get("title", "untitled"))
        
        if item_type == "file":
            # Download file
            content_id = item.get("content_id")
            if content_id:
                file_info = await self.api_get(f"/courses/{course_id}/files/{content_id}")
                if file_info:
                    await self.download_file(file_info, dest_dir)
        
        elif item_type == "page":
            # Download page content
            page_url = item.get("page_url")
            if page_url:
                page = await self.api_get(f"/courses/{course_id}/pages/{page_url}")
                if page:
                    await self.save_page(page, dest_dir, title)
        
        elif item_type == "assignment":
            # Download assignment details
            content_id = item.get("content_id")
            if content_id:
                assignment = await self.api_get(f"/courses/{course_id}/assignments/{content_id}")
                if assignment:
                    await self.save_assignment(assignment, dest_dir, title)
        
        elif item_type == "quiz":
            content_id = item.get("content_id")
            if content_id:
                quiz = await self.api_get(f"/courses/{course_id}/quizzes/{content_id}")
                if quiz:
                    await self.save_quiz(quiz, dest_dir, title, course_id)
        
        elif item_type == "externalurl":
            # Save external URL
            url = item.get("external_url", "")
            with open(dest_dir / f"{title}.url", "w") as f:
                f.write(f"[InternetShortcut]\nURL={url}\n")
            
            self.content_manifest.append({
                "content_type": "external_url",
                "title": item.get("title"),
                "url": url,
                "module": module_name,
                "synced_at": datetime.now().isoformat()
            })
        
        elif item_type == "subheader":
            # Just a header, skip
            pass
        
        else:
            print(f"      ⚠️ Unknown item type: {item_type}")
    
    async def download_files(self, course_id: int, course_dir: Path):
        """Download files from root folder only (module files already downloaded)."""
        print("📁 Checking root folder files...")
        files_dir = course_dir / "files"
        files_dir.mkdir(exist_ok=True)
        
        # Only get root folder files (not all 2000+ files across all folders)
        # Files in modules are already downloaded via download_module_item
        try:
            # Get root folder
            folders = await self.api_get(f"/courses/{course_id}/folders/root")
            if folders:
                root_id = folders.get("id")
                files = await self.api_get(f"/folders/{root_id}/files") or []
                
                if files:
                    print(f"   Found {len(files)} root folder files")
                    for file_info in files[:50]:  # Limit to 50 files max
                        await self.download_file(file_info, files_dir)
                else:
                    print("   No root folder files")
        except Exception as e:
            print(f"   ⚠️ Skipping files folder: {e}")
    
    async def download_file(self, file_info: dict, dest_dir: Path):
        """Download a single file (incremental - checks updated_at)."""
        filename = file_info.get("display_name") or file_info.get("filename", "unknown")
        url = file_info.get("url")
        file_id = file_info.get("id")
        updated_at = file_info.get("updated_at") or file_info.get("modified_at", "")
        
        if not url or url in self.downloaded_files:
            return
        
        filepath = dest_dir / self.sanitize_filename(filename)
        
        # Check if file needs update (incremental sync)
        if file_id and not self.needs_update(self.current_course_state, f"file_{file_id}", updated_at, filepath):
            return  # Skip - unchanged
        
        try:
            async with httpx.AsyncClient(cookies=self.cookies, follow_redirects=True, 
                                          timeout=300) as client:
                resp = await client.get(url)
                if resp.status_code == 200:
                    with open(filepath, "wb") as f:
                        f.write(resp.content)
                    print(f"      ✅ {filename}")
                    self.downloaded_files.add(url)
                    
                    # Extract text from PDFs for RAG/study
                    if filepath.suffix.lower() == '.pdf':
                        self.extract_pdf_text(filepath)
                    
                    # Mark as downloaded in sync state
                    if file_id:
                        self.mark_downloaded(self.current_course_state, f"file_{file_id}", updated_at, str(filepath))
                    
                    self.content_manifest.append({
                        "content_type": "file",
                        "title": filename,
                        "file_path": str(filepath.relative_to(DOWNLOAD_DIR)),
                        "size": file_info.get("size"),
                        "content_type": file_info.get("content-type"),
                        "synced_at": datetime.now().isoformat()
                    })
                else:
                    print(f"      ❌ {filename}: HTTP {resp.status_code}")
        except Exception as e:
            print(f"      ❌ {filename}: {e}")
    
    async def download_pages(self, course_id: int, course_dir: Path):
        """Download all wiki pages."""
        print("📄 Downloading pages...")
        pages_dir = course_dir / "pages"
        pages_dir.mkdir(exist_ok=True)
        
        pages = await self.api_get_paginated(f"/courses/{course_id}/pages")
        
        if not pages:
            print("   No pages found")
            return
        
        print(f"   Found {len(pages)} pages")
        
        for page_summary in pages:
            page_url = page_summary.get("url")
            if page_url:
                page = await self.api_get(f"/courses/{course_id}/pages/{page_url}")
                if page:
                    title = self.sanitize_filename(page.get("title", page_url))
                    await self.save_page(page, pages_dir, title)
    
    async def save_page(self, page: dict, dest_dir: Path, title: str):
        """Save a page as clean, readable text (incremental - checks updated_at)."""
        body = page.get("body", "")
        if not body:
            return
        
        page_id = page.get("page_id") or page.get("url", title)
        updated_at = page.get("updated_at", "")
        filepath = dest_dir / f"{title}.txt"
        
        # Check if page needs update
        if not self.needs_update(self.current_course_state, f"page_{page_id}", updated_at, filepath):
            return  # Skip - unchanged
        
        # Convert HTML to clean text
        text_content, links = html_to_text(body)
        
        # Build readable text file
        output = []
        output.append("=" * 60)
        output.append(page.get('title', title).upper())
        output.append("=" * 60)
        output.append("")
        output.append(text_content)
        
        # Add links section if any
        if links:
            output.append("")
            output.append("-" * 40)
            output.append("LINKS:")
            for link in links:
                output.append(f"  • {link['url']}")
        
        with open(filepath, "w", encoding="utf-8") as f:
            f.write("\n".join(output))
        
        # Mark as downloaded
        self.mark_downloaded(self.current_course_state, f"page_{page_id}", updated_at, str(filepath))
        print(f"      📄 {title}")
        
        self.content_manifest.append({
            "content_type": "page",
            "title": page.get("title"),
            "file_path": str(filepath.relative_to(DOWNLOAD_DIR)),
            "text_content": text_content[:2000],
            "links": links,
            "synced_at": datetime.now().isoformat()
        })
    
    async def download_assignments(self, course_id: int, course_dir: Path):
        """Download all assignments."""
        print("📝 Downloading assignments...")
        assignments_dir = course_dir / "assignments"
        assignments_dir.mkdir(exist_ok=True)
        
        assignments = await self.api_get_paginated(f"/courses/{course_id}/assignments")
        
        if not assignments:
            print("   No assignments found")
            return
        
        print(f"   Found {len(assignments)} assignments")
        
        for assignment in assignments:
            title = self.sanitize_filename(assignment.get("name", "untitled"))
            await self.save_assignment(assignment, assignments_dir, title)
    
    async def save_assignment(self, assignment: dict, dest_dir: Path, title: str):
        """Save an assignment as clean, readable text."""
        description = assignment.get("description", "") or ""
        
        # Convert HTML description to text
        text_content, links = html_to_text(description) if description else ("", [])
        
        # Build readable text file
        output = []
        output.append("=" * 60)
        output.append(f"ASSIGNMENT: {assignment.get('name', title).upper()}")
        output.append("=" * 60)
        output.append("")
        output.append(f"Due: {assignment.get('due_at', 'No due date')}")
        output.append(f"Points: {assignment.get('points_possible', 'N/A')}")
        output.append(f"Submission: {', '.join(assignment.get('submission_types', []))}")
        output.append("")
        output.append("-" * 40)
        output.append("")
        if text_content:
            output.append(text_content)
        else:
            output.append("(No description provided)")
        
        if links:
            output.append("")
            output.append("-" * 40)
            output.append("LINKS:")
            for link in links:
                output.append(f"  • {link['url']}")
        
        filepath = dest_dir / f"{title}.txt"
        with open(filepath, "w", encoding="utf-8") as f:
            f.write("\n".join(output))
        
        print(f"      📝 {title}")
        
        self.content_manifest.append({
            "content_type": "assignment",
            "title": assignment.get("name"),
            "due_date": assignment.get("due_at"),
            "points": assignment.get("points_possible"),
            "file_path": str(filepath.relative_to(DOWNLOAD_DIR)),
            "text_content": text_content[:2000],
            "synced_at": datetime.now().isoformat()
        })
    
    async def download_syllabus(self, course_id: int, course_dir: Path):
        """Download course syllabus as clean text."""
        print("📋 Downloading syllabus...")
        
        course = await self.api_get(f"/courses/{course_id}", {"include[]": ["syllabus_body"]})
        
        if not course or not course.get("syllabus_body"):
            print("   No syllabus found")
            return
        
        # Convert HTML to clean text
        text_content, links = html_to_text(course.get('syllabus_body', ''))
        
        output = []
        output.append("=" * 60)
        output.append(f"SYLLABUS: {course.get('name', '').upper()}")
        output.append("=" * 60)
        output.append("")
        output.append(text_content)
        
        if links:
            output.append("")
            output.append("-" * 40)
            output.append("LINKS:")
            for link in links:
                output.append(f"  • {link['url']}")
        
        filepath = course_dir / "syllabus.txt"
        with open(filepath, "w", encoding="utf-8") as f:
            f.write("\n".join(output))
        
        print("      ✅ Syllabus saved")
        
        self.content_manifest.append({
            "content_type": "syllabus",
            "title": "Course Syllabus",
            "file_path": str(filepath.relative_to(DOWNLOAD_DIR)),
            "text_content": text_content[:2000],
            "synced_at": datetime.now().isoformat()
        })
    
    async def download_announcements(self, course_id: int, course_dir: Path):
        """Download announcements."""
        print("📢 Downloading announcements...")
        announcements_dir = course_dir / "announcements"
        announcements_dir.mkdir(exist_ok=True)
        
        announcements = await self.api_get_paginated(
            "/announcements",
            {"context_codes[]": f"course_{course_id}"}
        )
        
        if not announcements:
            print("   No announcements found")
            return
        
        print(f"   Found {len(announcements)} announcements")
        
        for ann in announcements:
            title = self.sanitize_filename(ann.get("title", "untitled"))
            message = ann.get('message', '')
            
            # Convert HTML message to clean text
            text_content, links = html_to_text(message) if message else ("", [])
            
            output = []
            output.append("=" * 60)
            output.append(f"ANNOUNCEMENT: {ann.get('title', title).upper()}")
            output.append("=" * 60)
            output.append(f"Posted: {ann.get('posted_at', 'Unknown')}")
            output.append("")
            output.append(text_content if text_content else "(No content)")
            
            if links:
                output.append("")
                output.append("-" * 40)
                output.append("LINKS:")
                for link in links:
                    output.append(f"  • {link['url']}")
            
            filepath = announcements_dir / f"{title}.txt"
            with open(filepath, "w", encoding="utf-8") as f:
                f.write("\n".join(output))
            
            self.content_manifest.append({
                "content_type": "announcement",
                "title": ann.get("title"),
                "posted_at": ann.get("posted_at"),
                "file_path": str(filepath.relative_to(DOWNLOAD_DIR)),
                "text_content": text_content[:2000],
                "synced_at": datetime.now().isoformat()
            })
    
    async def download_quizzes(self, course_id: int, course_dir: Path):
        """Download quizzes and their questions."""
        print("❓ Downloading quizzes...")
        quizzes_dir = course_dir / "quizzes"
        quizzes_dir.mkdir(exist_ok=True)
        
        quizzes = await self.api_get_paginated(f"/courses/{course_id}/quizzes")
        
        if not quizzes:
            print("   No quizzes found")
            return
        
        print(f"   Found {len(quizzes)} quizzes")
        
        for quiz in quizzes:
            title = self.sanitize_filename(quiz.get("title", "untitled"))
            await self.save_quiz(quiz, quizzes_dir, title, course_id)
    
    async def save_quiz(self, quiz: dict, dest_dir: Path, title: str, course_id: int):
        """Save a quiz with its questions as clean text."""
        quiz_id = quiz.get("id")
        
        # Try to get quiz questions (may fail if not submitted)
        questions = []
        try:
            questions = await self.api_get_paginated(
                f"/courses/{course_id}/quizzes/{quiz_id}/questions"
            ) or []
        except:
            pass
        
        # Convert description HTML to text
        description = quiz.get('description', '') or ''
        desc_text, _ = html_to_text(description) if description else ("", [])
        
        # Build clean text output
        output = []
        output.append("=" * 60)
        output.append(f"QUIZ: {quiz.get('title', title).upper()}")
        output.append("=" * 60)
        output.append("")
        output.append(f"Due: {quiz.get('due_at', 'No due date')}")
        output.append(f"Time Limit: {quiz.get('time_limit', 'None')} minutes")
        output.append(f"Points: {quiz.get('points_possible', 'N/A')}")
        output.append(f"Questions: {quiz.get('question_count', len(questions))}")
        output.append("")
        
        if desc_text:
            output.append(desc_text)
            output.append("")
        
        output.append("-" * 40)
        output.append("QUESTIONS")
        output.append("-" * 40)
        
        if questions:
            for i, q in enumerate(questions, 1):
                # Convert question HTML to text
                q_text, _ = html_to_text(q.get('question_text', ''))
                output.append(f"\n{i}. {q_text}")
                
                for j, ans in enumerate(q.get("answers", []), ord('A')):
                    ans_text = ans.get('text', '')
                    output.append(f"   {chr(j)}) {ans_text}")
        else:
            output.append("\n(Questions not available - quiz not yet taken)")
        
        filepath = dest_dir / f"{title}.txt"
        with open(filepath, "w", encoding="utf-8") as f:
            f.write("\n".join(output))
        
        print(f"      ❓ {title} ({len(questions)} questions)")
        
        self.content_manifest.append({
            "content_type": "quiz",
            "title": quiz.get("title"),
            "due_date": quiz.get("due_at"),
            "points": quiz.get("points_possible"),
            "question_count": len(questions),
            "file_path": str(filepath.relative_to(DOWNLOAD_DIR)),
            "synced_at": datetime.now().isoformat()
        })
    
    async def extract_all_pdfs(self, course_dir: Path):
        """Extract text from all PDFs in course folder."""
        if not PDF_SUPPORT:
            return
        
        pdfs = list(course_dir.rglob("*.pdf"))
        if not pdfs:
            return
        
        extracted = 0
        for pdf_path in pdfs:
            txt_path = pdf_path.with_suffix('.pdf.txt')
            if not txt_path.exists():
                if self.extract_pdf_text(pdf_path):
                    extracted += 1
        
        if extracted > 0:
            print(f"   📖 Extracted text from {extracted} PDFs")
    
    def extract_pdf_text(self, pdf_path: Path) -> bool:
        """Extract text from PDF and save as .txt file alongside it."""
        if not PDF_SUPPORT:
            return False
        
        txt_path = pdf_path.with_suffix('.pdf.txt')
        
        # Skip if already extracted
        if txt_path.exists():
            return True
        
        try:
            doc = fitz.open(str(pdf_path))
            
            output = []
            output.append("=" * 60)
            output.append(f"EXTRACTED TEXT: {pdf_path.stem.upper()}")
            output.append("=" * 60)
            output.append(f"Pages: {len(doc)}")
            output.append("")
            
            for page_num, page in enumerate(doc, 1):
                text = page.get_text()
                if text.strip():
                    output.append(f"\n{'─' * 40}")
                    output.append(f"PAGE {page_num}")
                    output.append(f"{'─' * 40}\n")
                    output.append(text.strip())
            
            doc.close()
            
            with open(txt_path, "w", encoding="utf-8") as f:
                f.write("\n".join(output))
            
            print(f"      📖 Extracted: {pdf_path.name} → .txt")
            return True
            
        except Exception as e:
            print(f"      ⚠️ Could not extract {pdf_path.name}: {e}")
            return False
    
    def sanitize_filename(self, name: str) -> str:
        """Make string safe for filename."""
        name = re.sub(r'[<>:"/\\|?*]', '_', str(name))
        name = re.sub(r'\s+', ' ', name)
        name = name.strip('. ')
        return name[:100]


async def main():
    import sys
    
    # Check for --force flag
    force_full_sync = "--force" in sys.argv or "-f" in sys.argv
    
    mode = "FULL SYNC" if force_full_sync else "INCREMENTAL SYNC"
    print(f"""
╔═══════════════════════════════════════════════════════════╗
║    Canvas API Downloader for SRJC ({mode})     ║
╚═══════════════════════════════════════════════════════════╝
    """)
    
    if not force_full_sync:
        print("💡 Tip: Use --force to re-download all content")
    
    DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)
    print(f"📂 Download directory: {DOWNLOAD_DIR.absolute()}")
    
    downloader = CanvasAPIDownloader(force_full_sync=force_full_sync)
    
    if not downloader.load_session():
        return
    
    courses = await downloader.get_courses()
    
    if not courses:
        print("❌ No courses found!")
        return
    
    for course in courses:
        await downloader.download_course(course)
    
    # Save sync state for next incremental run
    downloader.save_sync_state()
    
    print(f"\n{'='*60}")
    print("✅ Sync complete!")
    print(f"📂 Files saved to: {DOWNLOAD_DIR.absolute()}")
    print(f"📊 Files downloaded this run: {len(downloader.downloaded_files)}")
    print(f"📅 Next run will only download new/changed content")


if __name__ == "__main__":
    asyncio.run(main())
