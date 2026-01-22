"""
Canvas Content Sync - Comprehensive Course Downloader
Downloads ALL content from Canvas courses with proper tracking to avoid re-downloads.

Features:
- Tracks every item by ID + updated_at timestamp
- Focuses on modules (where most content lives)
- Extracts all embedded links (files, videos, external URLs)
- Batches downloads per course
- Incremental sync by default (only downloads new/changed content)

Usage:
    python canvas_sync.py              # Incremental sync
    python canvas_sync.py --force      # Force re-download everything
    python canvas_sync.py --course "FDNT 10"  # Sync specific course
"""

import os
import json
import asyncio
import re
import hashlib
from pathlib import Path
from datetime import datetime
from urllib.parse import urljoin, unquote, urlparse
from html.parser import HTMLParser
from dataclasses import dataclass, asdict
from typing import Optional
import httpx
from dotenv import load_dotenv

# PDF text extraction
try:
    import fitz  # PyMuPDF
    PDF_SUPPORT = True
except ImportError:
    PDF_SUPPORT = False

load_dotenv()

# Configuration
CANVAS_URL = "https://canvas.santarosa.edu"
SESSION_FILE = Path(__file__).parent / ".canvas_session.json"

# Default to Google Drive on Mac, fallback to local
def get_download_dir():
    """Find Google Drive folder on Mac or fallback to local."""
    if os.getenv("DOWNLOAD_DIR"):
        return Path(os.getenv("DOWNLOAD_DIR"))
    
    home = Path.home()
    cloud_storage = home / "Library" / "CloudStorage"
    
    if cloud_storage.exists():
        for folder in cloud_storage.iterdir():
            if folder.name.startswith("GoogleDrive"):
                gdrive_path = folder / "My Drive" / "Canvas"
                gdrive_path.mkdir(parents=True, exist_ok=True)
                return gdrive_path
    
    # Fallback to local
    local = Path(__file__).parent / "canvas_downloads"
    local.mkdir(parents=True, exist_ok=True)
    return local


DOWNLOAD_DIR = get_download_dir()


class LinkExtractor(HTMLParser):
    """Extract all links and content from HTML."""
    
    def __init__(self):
        super().__init__()
        self.text_parts = []
        self.links = []  # All links found
        self.file_links = []  # Direct file download links
        self.video_links = []  # Video embeds
        self.external_links = []  # External URLs
        self.current_link = None
        self.in_script = False
        self.in_style = False
    
    def handle_starttag(self, tag, attrs):
        attrs_dict = dict(attrs)
        
        if tag == 'script':
            self.in_script = True
        elif tag == 'style':
            self.in_style = True
        elif tag == 'a':
            href = attrs_dict.get('href', '')
            if href and not href.startswith('#') and not href.startswith('javascript:'):
                self.current_link = href
                self._categorize_link(href, attrs_dict.get('title', ''))
        elif tag == 'iframe':
            src = attrs_dict.get('src', '')
            if src:
                self._handle_iframe(src, attrs_dict)
        elif tag == 'video':
            src = attrs_dict.get('src', '')
            if src:
                self.video_links.append({'url': src, 'type': 'video_tag'})
        elif tag == 'source':
            src = attrs_dict.get('src', '')
            if src and attrs_dict.get('type', '').startswith('video'):
                self.video_links.append({'url': src, 'type': 'video_source'})
        elif tag == 'br':
            self.text_parts.append('\n')
        elif tag in ('p', 'div'):
            self.text_parts.append('\n\n')
        elif tag in ('h1', 'h2', 'h3', 'h4', 'h5', 'h6'):
            self.text_parts.append('\n\n')
        elif tag == 'li':
            self.text_parts.append('\n• ')
    
    def _categorize_link(self, href: str, title: str = ''):
        """Categorize a link by type."""
        link_info = {'url': href, 'title': title}
        
        # Canvas file links
        if '/files/' in href:
            self.file_links.append(link_info)
        # Video platforms
        elif any(v in href for v in ['youtube.com', 'youtu.be', 'vimeo.com', 'kaltura', 'panopto']):
            self.video_links.append({**link_info, 'type': 'external_video'})
        # Media objects
        elif '/media_objects/' in href or '/media/' in href:
            self.video_links.append({**link_info, 'type': 'canvas_media'})
        # External links
        elif href.startswith('http') and 'canvas.santarosa.edu' not in href:
            self.external_links.append(link_info)
        
        self.links.append(link_info)
    
    def _handle_iframe(self, src: str, attrs: dict):
        """Handle iframe embeds (often videos)."""
        if any(v in src for v in ['youtube', 'vimeo', 'kaltura', 'panopto', 'media']):
            self.video_links.append({
                'url': src,
                'type': 'iframe_embed',
                'title': attrs.get('title', '')
            })
    
    def handle_endtag(self, tag):
        if tag == 'script':
            self.in_script = False
        elif tag == 'style':
            self.in_style = False
        elif tag == 'a':
            self.current_link = None
    
    def handle_data(self, data):
        if self.in_script or self.in_style:
            return
        text = data.strip()
        if text:
            self.text_parts.append(text)
    
    def get_text(self) -> str:
        """Get clean text."""
        text = ' '.join(self.text_parts)
        text = re.sub(r'\n\s*\n\s*\n+', '\n\n', text)
        text = re.sub(r' +', ' ', text)
        return text.strip()


def extract_content(html: str) -> dict:
    """Extract text and all links from HTML content."""
    parser = LinkExtractor()
    try:
        parser.feed(html)
        return {
            'text': parser.get_text(),
            'all_links': parser.links or [],
            'file_links': parser.file_links or [],
            'video_links': parser.video_links or [],
            'external_links': parser.external_links or []
        }
    except Exception:
        # Fallback - always return all keys
        text = re.sub(r'<[^>]+>', ' ', html)
        return {
            'text': text.strip(), 
            'all_links': [], 
            'file_links': [], 
            'video_links': [], 
            'external_links': []
        }


@dataclass
class SyncItem:
    """Represents a tracked content item."""
    item_id: str
    item_type: str  # file, page, module, quiz, assignment, announcement
    title: str
    updated_at: str
    file_path: Optional[str] = None
    source_url: Optional[str] = None
    synced_at: Optional[str] = None
    content_hash: Optional[str] = None
    file_size: Optional[int] = None
    links: Optional[list] = None


class SyncTracker:
    """Tracks all synced items to enable incremental updates."""
    
    def __init__(self, course_dir: Path):
        self.course_dir = course_dir
        self.state_file = course_dir / "_sync_state.json"
        self.state = self._load()
    
    def _load(self) -> dict:
        """Load sync state from file."""
        if self.state_file.exists():
            try:
                with open(self.state_file) as f:
                    return json.load(f)
            except Exception:
                pass
        return {
            "last_sync": None,
            "items": {},  # item_id -> SyncItem data
            "stats": {"total": 0, "files": 0, "pages": 0, "skipped": 0}
        }
    
    def save(self):
        """Save sync state."""
        self.state["last_sync"] = datetime.now().isoformat()
        with open(self.state_file, "w") as f:
            json.dump(self.state, f, indent=2, default=str)
    
    def needs_sync(self, item_id: str, updated_at: str, file_path: Path = None) -> bool:
        """Check if an item needs to be synced."""
        existing = self.state["items"].get(item_id)
        
        if not existing:
            return True  # New item
        
        if existing.get("updated_at") != updated_at:
            return True  # Content changed
        
        if file_path and not file_path.exists():
            return True  # File missing
        
        return False
    
    def mark_synced(self, item: SyncItem):
        """Mark an item as synced."""
        item.synced_at = datetime.now().isoformat()
        self.state["items"][item.item_id] = asdict(item)
    
    def get_stats(self) -> dict:
        """Get sync statistics."""
        items = self.state["items"]
        return {
            "total_items": len(items),
            "files": sum(1 for i in items.values() if i.get("item_type") == "file"),
            "pages": sum(1 for i in items.values() if i.get("item_type") == "page"),
            "modules": sum(1 for i in items.values() if i.get("item_type") == "module"),
            "last_sync": self.state.get("last_sync")
        }


class CanvasSync:
    """Main Canvas content syncer."""
    
    def __init__(self, force_sync: bool = False, course_filter: str = None):
        self.force_sync = force_sync
        self.course_filter = course_filter.lower() if course_filter else None
        self.cookies = {}
        self.headers = {
            "Accept": "application/json",
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"
        }
        self.tracker: SyncTracker = None
        self.current_course_dir: Path = None
        self.stats = {"new": 0, "updated": 0, "skipped": 0, "errors": 0}
    
    def load_session(self) -> bool:
        """Load session cookies."""
        if not SESSION_FILE.exists():
            print("❌ No session file found!")
            print("   Run 'HEADLESS=false python canvas_downloader.py' first to login")
            return False
        
        with open(SESSION_FILE) as f:
            data = json.load(f)
        
        self.cookies = {c["name"]: c["value"] for c in data.get("cookies", [])}
        print(f"✅ Loaded session ({len(self.cookies)} cookies)")
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
                elif resp.status_code == 401:
                    print(f"   ⚠️ Session expired - need to re-login")
                    return None
                else:
                    return None
            except Exception as e:
                print(f"   ⚠️ API error: {e}")
                return None
    
    async def api_get_all(self, endpoint: str, params: dict = None) -> list:
        """Get all pages of a paginated response."""
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
                    
                    # Get next page from Link header
                    links = resp.headers.get("Link", "")
                    url = None
                    for link in links.split(","):
                        if 'rel="next"' in link:
                            url = link.split(";")[0].strip("<> ")
                            break
                    params = {}
                except Exception as e:
                    break
        
        return results
    
    async def download_file(self, url: str, dest_path: Path, item_id: str = None,
                           updated_at: str = None, title: str = None, 
                           course_id: int = None, file_id: int = None) -> bool:
        """Download a file with tracking."""
        try:
            # Skip if already synced and not changed
            if item_id and not self.force_sync:
                if not self.tracker.needs_sync(item_id, updated_at or "", dest_path):
                    self.stats["skipped"] += 1
                    return True
            
            async with httpx.AsyncClient(cookies=self.cookies, follow_redirects=True,
                                          timeout=300) as client:
                resp = await client.get(url)
                
                if resp.status_code == 200:
                    # Get actual filename from Content-Disposition if available
                    cd = resp.headers.get("content-disposition", "")
                    if "filename=" in cd:
                        match = re.search(r'filename\*?=["\']?(?:UTF-8\'\')?([^";\n\r\']+)', cd, re.IGNORECASE)
                        if match:
                            actual_name = unquote(match.group(1))
                            dest_path = dest_path.parent / self.sanitize_filename(actual_name)
                    
                    dest_path.parent.mkdir(parents=True, exist_ok=True)
                    with open(dest_path, "wb") as f:
                        f.write(resp.content)
                    
                    # Check if it's a PowerPoint file - add Canvas page link for inline videos
                    is_powerpoint = dest_path.suffix.lower() in ['.ppt', '.pptx']
                    canvas_file_url = None
                    if is_powerpoint and course_id and file_id:
                        canvas_file_url = f"{CANVAS_URL}/courses/{course_id}/files/{file_id}"
                        # Create a companion file with the Canvas link
                        link_file = dest_path.with_suffix(dest_path.suffix + '.canvas_link.txt')
                        with open(link_file, "w", encoding="utf-8") as f:
                            f.write("=" * 60 + "\n")
                            f.write(f"CANVAS PAGE LINK FOR: {dest_path.name}\n")
                            f.write("=" * 60 + "\n\n")
                            f.write("This PowerPoint may contain inline videos.\n")
                            f.write("View it on Canvas to access embedded video content:\n\n")
                            f.write(f"🔗 {canvas_file_url}\n\n")
                            f.write("Note: Inline videos in PowerPoint files cannot be extracted.\n")
                            f.write("Please view this file on Canvas to see any embedded videos.\n")
                    
                    # Track the download
                    tracked_links = []
                    if canvas_file_url:
                        tracked_links.append({
                            'url': canvas_file_url,
                            'title': 'Canvas File Page (for inline videos)',
                            'type': 'canvas_file_page'
                        })
                    
                    if item_id:
                        self.tracker.mark_synced(SyncItem(
                            item_id=item_id,
                            item_type="file",
                            title=title or dest_path.name,
                            updated_at=updated_at or "",
                            file_path=str(dest_path.relative_to(DOWNLOAD_DIR)),
                            source_url=canvas_file_url or url,
                            file_size=len(resp.content),
                            links=tracked_links if tracked_links else None
                        ))
                        self.stats["new"] += 1
                    
                    if is_powerpoint:
                        print(f"      ✅ {dest_path.name} (PowerPoint - Canvas link saved)")
                    else:
                        print(f"      ✅ {dest_path.name}")
                    return True
                else:
                    return False
                    
        except Exception as e:
            print(f"      ❌ Download error: {e}")
            self.stats["errors"] += 1
            return False
    
    async def get_courses(self) -> list:
        """Get enrolled courses."""
        print("📚 Fetching courses...")
        
        # Try active courses first
        courses = await self.api_get_all("/courses", {
            "enrollment_state": "active",
            "include[]": ["term"]
        })
        
        # If no active courses, try all enrollments
        if not courses:
            print("   No active courses, trying all enrollments...")
            courses = await self.api_get_all("/courses", {
                "include[]": ["term"]
            })
        
        if not courses:
            print("   ⚠️ No courses returned from API")
            # Try a simple test API call
            test = await self.api_get("/users/self")
            if test:
                print("   ✅ API is working, but no courses found")
            else:
                print("   ❌ API authentication failed - session may be expired")
            return []
        
        # Filter courses
        filtered = []
        for c in courses:
            name = c.get("name", "")
            if not name:
                continue
            
            # Apply course filter if specified
            if self.course_filter and self.course_filter not in name.lower():
                continue
            
            filtered.append(c)
            print(f"   📖 {name}")
        
        return filtered
    
    async def sync_course(self, course: dict):
        """Sync all content from a course."""
        course_id = course["id"]
        course_name = self.sanitize_filename(course.get("name", f"Course_{course_id}"))
        
        self.current_course_dir = DOWNLOAD_DIR / course_name
        self.current_course_dir.mkdir(parents=True, exist_ok=True)
        
        # Initialize tracker for this course
        self.tracker = SyncTracker(self.current_course_dir)
        self.stats = {"new": 0, "updated": 0, "skipped": 0, "errors": 0}
        
        print(f"\n{'='*60}")
        print(f"📖 Syncing: {course_name}")
        print(f"{'='*60}")
        
        last_sync = self.tracker.state.get("last_sync")
        if last_sync and not self.force_sync:
            print(f"   📅 Last sync: {last_sync[:16]}")
        
        # MODULES ARE PRIMARY - sync them first and thoroughly
        await self.sync_modules(course_id)
        
        # Then other content types
        await self.sync_pages(course_id)
        await self.sync_assignments(course_id)
        await self.sync_syllabus(course_id, course_name)
        await self.sync_announcements(course_id)
        await self.sync_quizzes(course_id)
        await self.sync_root_files(course_id)
        
        # Save tracker state
        self.tracker.save()
        
        # Create manifest for other tools
        await self.create_manifest(course)
        
        # Print summary
        print(f"\n   📊 Summary: {self.stats['new']} new, {self.stats['skipped']} unchanged, {self.stats['errors']} errors")
    
    async def sync_modules(self, course_id: int):
        """Sync all modules - THE PRIMARY CONTENT SOURCE.
        Handles locked/unreleased modules and checks for newly released ones."""
        print("\n📦 Syncing modules (primary content source)...")
        
        modules_dir = self.current_course_dir / "modules"
        modules_dir.mkdir(exist_ok=True)
        
        # Get all modules with items included - this includes locked/unreleased modules
        modules = await self.api_get_all(f"/courses/{course_id}/modules", {
            "include[]": ["items", "content_details"]
        })
        
        if not modules:
            print("   No modules found")
            return
        
        print(f"   Found {len(modules)} modules (including locked/unreleased)")
        
        # Track previously synced modules to detect new releases
        previously_synced_modules = {
            item_id.replace("module_", ""): item 
            for item_id, item in self.tracker.state.get("items", {}).items()
            if item.get("item_type") == "module"
        }
        
        for module in modules:
            module_id = module.get("id")
            module_name = self.sanitize_filename(module.get("name", f"Module_{module_id}"))
            module_dir = modules_dir / module_name
            module_dir.mkdir(exist_ok=True)
            
            # Check module state
            state = module.get("state", "unknown")
            published = module.get("published", True)
            unlock_at = module.get("unlock_at")
            require_sequential_progress = module.get("require_sequential_progress", False)
            
            # Determine if module is accessible
            now_iso = datetime.now().isoformat()
            is_locked = state in ["locked", "unlocked"] and not published
            is_unreleased = unlock_at and unlock_at > now_iso
            is_accessible = published and (not unlock_at or unlock_at <= now_iso)
            
            # Check if this is a newly released module
            was_previously_locked = str(module_id) in previously_synced_modules
            is_newly_released = was_previously_locked and is_accessible
            
            status_icon = "🔓" if is_accessible else "🔒"
            if is_newly_released:
                status_icon = "🆕"
            
            print(f"\n   {status_icon} {module_name}")
            
            if not is_accessible:
                if unlock_at:
                    print(f"      ⏳ Unlocks: {unlock_at[:10]}")
                elif not published:
                    print(f"      🔒 Not published yet")
                else:
                    print(f"      🔒 Locked (requires sequential progress)" if require_sequential_progress else "      🔒 Locked")
            
            # Track module itself (even if locked - so we can detect when it unlocks)
            self.tracker.mark_synced(SyncItem(
                item_id=f"module_{module_id}",
                item_type="module",
                title=module.get("name"),
                updated_at=module.get("updated_at", ""),
                file_path=str(module_dir.relative_to(DOWNLOAD_DIR)),
                source_url=f"{CANVAS_URL}/courses/{course_id}/modules/{module_id}" if course_id else None
            ))
            
            # Only sync content if module is accessible
            if not is_accessible:
                # Create a placeholder file for locked modules
                lock_info_file = module_dir / "_module_locked.txt"
                with open(lock_info_file, "w", encoding="utf-8") as f:
                    f.write("=" * 60 + "\n")
                    f.write(f"MODULE: {module.get('name', 'Unknown')}\n")
                    f.write("=" * 60 + "\n\n")
                    f.write("This module is currently locked or not yet released.\n\n")
                    if unlock_at:
                        f.write(f"Unlocks on: {unlock_at}\n")
                    if not published:
                        f.write("Status: Not published\n")
                    if require_sequential_progress:
                        f.write("Note: Requires completing previous modules in sequence.\n")
                    f.write(f"\nCanvas URL: {CANVAS_URL}/courses/{course_id}/modules/{module_id}\n")
                    f.write("\nThis module will be synced automatically when it becomes available.\n")
                print(f"      📝 Locked - will check again on next sync")
                continue
            
            # Get module items (may need separate API call)
            items = module.get("items", [])
            if not items:
                items = await self.api_get_all(
                    f"/courses/{course_id}/modules/{module_id}/items",
                    {"include[]": ["content_details"]}
                ) or []
            
            if is_newly_released:
                print(f"      🆕 NEWLY RELEASED! Syncing {len(items)} items...")
            else:
                print(f"      {len(items)} items")
            
            # Process each item in the module
            for item in items:
                await self.sync_module_item(course_id, item, module_dir, module_name)
    
    async def sync_module_item(self, course_id: int, item: dict, module_dir: Path, module_name: str):
        """Sync a single module item with full link extraction."""
        item_type = item.get("type", "").lower()
        item_id = item.get("id")
        title = item.get("title", "untitled")
        content_id = item.get("content_id")
        
        if item_type == "file":
            # Direct file download
            if content_id:
                file_info = await self.api_get(f"/courses/{course_id}/files/{content_id}")
                if file_info:
                    url = file_info.get("url")
                    filename = self.sanitize_filename(file_info.get("display_name", title))
                    await self.download_file(
                        url,
                        module_dir / filename,
                        item_id=f"file_{content_id}",
                        updated_at=file_info.get("updated_at"),
                        title=title,
                        course_id=course_id,
                        file_id=content_id
                    )
        
        elif item_type == "page":
            # Wiki page - save content and extract all links
            page_url = item.get("page_url")
            if page_url:
                page = await self.api_get(f"/courses/{course_id}/pages/{page_url}")
                if page:
                    await self.save_page_with_links(page, module_dir, title, course_id)
        
        elif item_type == "assignment":
            # Assignment details
            if content_id:
                assignment = await self.api_get(f"/courses/{course_id}/assignments/{content_id}")
                if assignment:
                    await self.save_assignment(assignment, module_dir, title, course_id)
        
        elif item_type == "quiz":
            # Quiz in module
            if content_id:
                quiz = await self.api_get(f"/courses/{course_id}/quizzes/{content_id}")
                if quiz:
                    await self.save_quiz(quiz, module_dir, title, course_id)
        
        elif item_type == "discussion":
            # Discussion topic
            if content_id:
                discussion = await self.api_get(f"/courses/{course_id}/discussion_topics/{content_id}")
                if discussion:
                    await self.save_discussion(discussion, module_dir, title, course_id)
        
        elif item_type == "externalurl":
            # External URL - save as .url file and track link
            url = item.get("external_url", "")
            if url:
                url_file = module_dir / f"{self.sanitize_filename(title)}.url"
                
                # Check if needs sync
                if not self.force_sync:
                    if not self.tracker.needs_sync(f"exturl_{item_id}", url, url_file):
                        self.stats["skipped"] += 1
                        return
                
                with open(url_file, "w") as f:
                    f.write(f"[InternetShortcut]\nURL={url}\n")
                
                # Track with link info
                self.tracker.mark_synced(SyncItem(
                    item_id=f"exturl_{item_id}",
                    item_type="external_url",
                    title=title,
                    updated_at=url,  # Use URL as version identifier
                    file_path=str(url_file.relative_to(DOWNLOAD_DIR)),
                    source_url=url,
                    links=[{"url": url, "title": title, "type": "external_url"}]
                ))
                print(f"      🔗 {title}")
                self.stats["new"] += 1
        
        elif item_type == "externaltool":
            # External tool (might be video embed, LTI content, etc.)
            url = item.get("url") or item.get("external_url", "")
            if url:
                url_file = module_dir / f"{self.sanitize_filename(title)}_tool.url"
                
                # Check if needs sync
                if not self.force_sync:
                    if not self.tracker.needs_sync(f"exttool_{item_id}", url, url_file):
                        self.stats["skipped"] += 1
                        return
                
                # Save the link
                with open(url_file, "w") as f:
                    f.write(f"[InternetShortcut]\nURL={url}\n")
                
                # Track with link info
                self.tracker.mark_synced(SyncItem(
                    item_id=f"exttool_{item_id}",
                    item_type="external_tool",
                    title=title,
                    updated_at=url,
                    file_path=str(url_file.relative_to(DOWNLOAD_DIR)),
                    source_url=url,
                    links=[{"url": url, "title": title, "type": "external_tool"}]
                ))
                print(f"      🔧 {title} (external tool)")
                self.stats["new"] += 1
        
        elif item_type == "subheader":
            # Just a section header, skip
            pass
    
    async def save_page_with_links(self, page: dict, dest_dir: Path, title: str, course_id: int):
        """Save a page and download all linked files."""
        body = page.get("body", "")
        if not body:
            return
        
        page_id = page.get("page_id") or page.get("url", title)
        updated_at = page.get("updated_at", "")
        
        # Check if needs sync
        if not self.force_sync:
            filepath = dest_dir / f"{self.sanitize_filename(title)}.txt"
            if not self.tracker.needs_sync(f"page_{page_id}", updated_at, filepath):
                self.stats["skipped"] += 1
                return
        
        # Extract content and all links
        content = extract_content(body)
        
        # Ensure all keys exist
        content.setdefault('file_links', [])
        content.setdefault('video_links', [])
        content.setdefault('external_links', [])
        content.setdefault('all_links', [])
        content.setdefault('text', '')
        
        # Save as readable text
        output = []
        output.append("=" * 60)
        output.append(page.get('title', title).upper())
        output.append("=" * 60)
        output.append("")
        output.append(content.get('text', ''))
        
        # Add links section
        if content.get('all_links'):
            output.append("")
            output.append("-" * 40)
            output.append("LINKS FOUND IN THIS PAGE:")
            output.append("-" * 40)
            
            for link in content.get('file_links', []):
                output.append(f"  📄 FILE: {link.get('url', '')}")
            for link in content.get('video_links', []):
                output.append(f"  🎥 VIDEO: {link.get('url', '')}")
            for link in content.get('external_links', []):
                output.append(f"  🔗 EXTERNAL: {link.get('url', '')}")
        
        filepath = dest_dir / f"{self.sanitize_filename(title)}.txt"
        with open(filepath, "w", encoding="utf-8") as f:
            f.write("\n".join(output))
        
        # Track with links
        self.tracker.mark_synced(SyncItem(
            item_id=f"page_{page_id}",
            item_type="page",
            title=page.get("title", title),
            updated_at=updated_at,
            file_path=str(filepath.relative_to(DOWNLOAD_DIR)),
            links=content.get('all_links', [])
        ))
        
        print(f"      📄 {title}")
        self.stats["new"] += 1
        
        # Download linked files
        # Build page URL for reference (in case files have inline videos)
        page_url = f"{CANVAS_URL}/courses/{course_id}/pages/{page_id}" if course_id and page_id else None
        
        for link in content.get('file_links', []):
            url = link.get('url', '')
            if not url:
                continue
            if url.startswith("/"):
                url = f"{CANVAS_URL}{url}"
            
            # Ensure it's a download link
            if "/files/" in url and "/download" not in url:
                url = url.rstrip("/") + "/download"
            
            # Extract file ID for tracking
            match = re.search(r'/files/(\d+)', url)
            file_id = match.group(1) if match else None
            
            filename = link.get('title', '').strip()
            if not filename or filename.lower() in ['here', 'click here', 'link', 'download']:
                filename = f"linked_file_{file_id}" if file_id else "linked_file"
            
            # Check if it's a PowerPoint - if so, we'll use the page URL instead of file URL
            # since the page might have inline videos
            is_powerpoint = any(filename.lower().endswith(ext) for ext in ['.ppt', '.pptx'])
            canvas_url_for_file = None
            
            if is_powerpoint and page_url:
                # For PowerPoints linked from pages, use the page URL (where inline videos might be)
                canvas_url_for_file = page_url
            elif is_powerpoint and file_id and course_id:
                # Fallback to file page URL
                canvas_url_for_file = f"{CANVAS_URL}/courses/{course_id}/files/{file_id}"
            
            await self.download_file(
                url,
                dest_dir / self.sanitize_filename(filename),
                item_id=f"linked_file_{file_id}" if file_id else None,
                title=filename,
                course_id=course_id,
                file_id=int(file_id) if file_id else None
            )
            
            # If it's a PowerPoint from a page, create additional note file
            if is_powerpoint and page_url:
                link_note_file = dest_dir / f"{self.sanitize_filename(filename)}.page_link.txt"
                with open(link_note_file, "w", encoding="utf-8") as f:
                    f.write("=" * 60 + "\n")
                    f.write(f"CANVAS PAGE LINK FOR: {filename}\n")
                    f.write("=" * 60 + "\n\n")
                    f.write("This PowerPoint was linked from a Canvas page and may contain inline videos.\n")
                    f.write("View it on the original Canvas page to access embedded video content:\n\n")
                    f.write(f"🔗 {page_url}\n\n")
                    f.write("Note: Inline videos in PowerPoint files cannot be extracted.\n")
                    f.write("Please view this file on the Canvas page to see any embedded videos.\n")
        
        # Save video links to a separate file
        if content.get('video_links'):
            videos_file = dest_dir / f"{self.sanitize_filename(title)}_videos.txt"
            with open(videos_file, "w") as f:
                f.write(f"Videos linked from: {title}\n")
                f.write("=" * 40 + "\n\n")
                for video in content.get('video_links', []):
                    f.write(f"Type: {video.get('type', 'unknown')}\n")
                    f.write(f"URL: {video.get('url', '')}\n")
                    if video.get('title'):
                        f.write(f"Title: {video.get('title')}\n")
                    f.write("\n")
    
    async def save_assignment(self, assignment: dict, dest_dir: Path, title: str, course_id: int = None):
        """Save assignment details and download all linked files."""
        description = assignment.get("description", "") or ""
        content = extract_content(description) if description else {
            'text': '', 
            'all_links': [], 
            'file_links': [], 
            'video_links': [], 
            'external_links': []
        }
        
        # Ensure all keys exist
        content.setdefault('file_links', [])
        content.setdefault('video_links', [])
        content.setdefault('external_links', [])
        content.setdefault('all_links', [])
        
        assignment_id = assignment.get('id')
        updated_at = assignment.get("updated_at", "")
        
        # Check if needs sync
        if not self.force_sync:
            filepath = dest_dir / f"{self.sanitize_filename(title)}.txt"
            if not self.tracker.needs_sync(f"assignment_{assignment_id}", updated_at, filepath):
                self.stats["skipped"] += 1
                return
        
        # Build direct Canvas URL
        canvas_url = f"{CANVAS_URL}/courses/{course_id}/assignments/{assignment_id}" if course_id else None
        
        output = []
        output.append("=" * 60)
        output.append(f"ASSIGNMENT: {assignment.get('name', title).upper()}")
        output.append("=" * 60)
        output.append("")
        if canvas_url:
            output.append(f"🔗 Direct URL: {canvas_url}")
            output.append("")
        output.append(f"Due: {assignment.get('due_at', 'No due date')}")
        output.append(f"Points: {assignment.get('points_possible', 'N/A')}")
        output.append(f"Submission: {', '.join(assignment.get('submission_types', []))}")
        output.append("")
        output.append("-" * 40)
        output.append("")
        output.append(content.get('text', '') if content.get('text') else "(No description)")
        
        # Add links section
        if content.get('all_links'):
            output.append("")
            output.append("-" * 40)
            output.append("LINKS FOUND IN THIS ASSIGNMENT:")
            output.append("-" * 40)
            for link in content.get('file_links', []):
                output.append(f"  📄 FILE: {link.get('url', '')}")
            for link in content.get('video_links', []):
                output.append(f"  🎥 VIDEO: {link.get('url', '')}")
            for link in content.get('external_links', []):
                output.append(f"  🔗 EXTERNAL: {link.get('url', '')}")
        
        filepath = dest_dir / f"{self.sanitize_filename(title)}.txt"
        with open(filepath, "w", encoding="utf-8") as f:
            f.write("\n".join(output))
        
        # Track with links (include Canvas URL)
        all_tracked_links = list(content.get('all_links', []))
        if canvas_url:
            all_tracked_links.insert(0, {
                'url': canvas_url,
                'title': 'Canvas Assignment Page',
                'type': 'canvas_assignment'
            })
        
        self.tracker.mark_synced(SyncItem(
            item_id=f"assignment_{assignment_id}",
            item_type="assignment",
            title=assignment.get("name"),
            updated_at=updated_at,
            file_path=str(filepath.relative_to(DOWNLOAD_DIR)),
            source_url=canvas_url,
            links=all_tracked_links
        ))
        
        print(f"      📝 {title}")
        self.stats["new"] += 1
        
        # Download linked files if course_id provided
        if course_id:
            for link in content.get('file_links', []):
                url = link.get('url', '')
                if not url:
                    continue
                    
                if url.startswith("/"):
                    url = f"{CANVAS_URL}{url}"
                
                # Ensure it's a download link
                if "/files/" in url and "/download" not in url:
                    url = url.rstrip("/") + "/download"
                
                # Extract file ID for tracking
                match = re.search(r'/files/(\d+)', url)
                file_id = match.group(1) if match else None
                
                filename = link.get('title', '').strip()
                if not filename or filename.lower() in ['here', 'click here', 'link', 'download']:
                    filename = f"linked_file_{file_id}" if file_id else "linked_file"
                
                await self.download_file(
                    url,
                    dest_dir / self.sanitize_filename(filename),
                    item_id=f"linked_file_{file_id}" if file_id else None,
                    title=filename,
                    course_id=course_id,
                    file_id=int(file_id) if file_id else None
                )
    
    async def save_quiz(self, quiz: dict, dest_dir: Path, title: str, course_id: int):
        """Save quiz details and questions if available, with full link extraction."""
        quiz_id = quiz.get("id")
        description = quiz.get("description", "") or ""
        content = extract_content(description) if description else {
            'text': '', 
            'all_links': [], 
            'file_links': [], 
            'video_links': [], 
            'external_links': []
        }
        
        # Ensure all keys exist
        content.setdefault('file_links', [])
        content.setdefault('video_links', [])
        content.setdefault('external_links', [])
        content.setdefault('all_links', [])
        
        updated_at = quiz.get("updated_at", "")
        
        # Check if needs sync
        if not self.force_sync:
            filepath = dest_dir / f"{self.sanitize_filename(title)}.txt"
            if not self.tracker.needs_sync(f"quiz_{quiz_id}", updated_at, filepath):
                self.stats["skipped"] += 1
                return
        
        # Try to get questions
        questions = await self.api_get_all(f"/courses/{course_id}/quizzes/{quiz_id}/questions") or []
        
        # Collect all links from questions too
        all_links = list(content.get('all_links', []))
        for q in questions:
            q_content = extract_content(q.get('question_text', ''))
            all_links.extend(q_content.get('all_links', []))
        
        # Build direct Canvas URL
        canvas_url = f"{CANVAS_URL}/courses/{course_id}/quizzes/{quiz_id}" if course_id else None
        
        output = []
        output.append("=" * 60)
        output.append(f"QUIZ: {quiz.get('title', title).upper()}")
        output.append("=" * 60)
        output.append("")
        if canvas_url:
            output.append(f"🔗 Direct URL: {canvas_url}")
            output.append("")
        output.append(f"Due: {quiz.get('due_at', 'No due date')}")
        output.append(f"Time Limit: {quiz.get('time_limit', 'None')} minutes")
        output.append(f"Points: {quiz.get('points_possible', 'N/A')}")
        output.append(f"Questions: {quiz.get('question_count', len(questions))}")
        output.append("")
        
        if content.get('text'):
            output.append(content.get('text', ''))
            output.append("")
        
        if questions:
            output.append("-" * 40)
            output.append("QUESTIONS:")
            output.append("-" * 40)
            for i, q in enumerate(questions, 1):
                q_content = extract_content(q.get('question_text', ''))
                output.append(f"\n{i}. {q_content.get('text', '')}")
                for j, ans in enumerate(q.get("answers", []), ord('A')):
                    output.append(f"   {chr(j)}) {ans.get('text', '')}")
        else:
            output.append("\n(Questions not available - quiz not yet taken)")
        
        # Add links section
        if all_links:
            output.append("")
            output.append("-" * 40)
            output.append("LINKS FOUND IN THIS QUIZ:")
            output.append("-" * 40)
            for link in content.get('file_links', []):
                output.append(f"  📄 FILE: {link.get('url', '')}")
            for link in content.get('video_links', []):
                output.append(f"  🎥 VIDEO: {link.get('url', '')}")
            for link in content.get('external_links', []):
                output.append(f"  🔗 EXTERNAL: {link.get('url', '')}")
        
        filepath = dest_dir / f"{self.sanitize_filename(title)}.txt"
        with open(filepath, "w", encoding="utf-8") as f:
            f.write("\n".join(output))
        
        # Track with all links (include Canvas URL - already defined above)
        all_tracked_links = list(all_links)
        if canvas_url:
            all_tracked_links.insert(0, {
                'url': canvas_url,
                'title': 'Canvas Quiz Page',
                'type': 'canvas_quiz'
            })
        
        self.tracker.mark_synced(SyncItem(
            item_id=f"quiz_{quiz_id}",
            item_type="quiz",
            title=quiz.get("title"),
            updated_at=updated_at,
            file_path=str(filepath.relative_to(DOWNLOAD_DIR)),
            source_url=canvas_url,
            links=all_tracked_links
        ))
        
        print(f"      ❓ {title} ({len(questions)} questions)")
        self.stats["new"] += 1
        
        # Download linked files
        for link in content.get('file_links', []):
            url = link.get('url', '')
            if not url:
                continue
                
            if url.startswith("/"):
                url = f"{CANVAS_URL}{url}"
            
            # Ensure it's a download link
            if "/files/" in url and "/download" not in url:
                url = url.rstrip("/") + "/download"
            
            # Extract file ID for tracking
            match = re.search(r'/files/(\d+)', url)
            file_id = match.group(1) if match else None
            
            filename = link.get('title', '').strip()
            if not filename or filename.lower() in ['here', 'click here', 'link', 'download']:
                filename = f"linked_file_{file_id}" if file_id else "linked_file"
            
            await self.download_file(
                url,
                dest_dir / self.sanitize_filename(filename),
                item_id=f"linked_file_{file_id}" if file_id else None,
                title=filename,
                course_id=course_id,
                file_id=int(file_id) if file_id else None
            )
    
    async def save_discussion(self, discussion: dict, dest_dir: Path, title: str, course_id: int = None):
        """Save discussion topic with full link extraction."""
        message = discussion.get("message", "") or ""
        content = extract_content(message) if message else {
            'text': '', 
            'all_links': [], 
            'file_links': [], 
            'video_links': [], 
            'external_links': []
        }
        
        # Ensure all keys exist
        content.setdefault('file_links', [])
        content.setdefault('video_links', [])
        content.setdefault('external_links', [])
        content.setdefault('all_links', [])
        
        discussion_id = discussion.get('id')
        updated_at = discussion.get("posted_at", "")
        
        # Check if needs sync
        if not self.force_sync:
            filepath = dest_dir / f"{self.sanitize_filename(title)}.txt"
            if not self.tracker.needs_sync(f"discussion_{discussion_id}", updated_at, filepath):
                self.stats["skipped"] += 1
                return
        
        output = []
        output.append("=" * 60)
        output.append(f"DISCUSSION: {discussion.get('title', title).upper()}")
        output.append("=" * 60)
        output.append("")
        output.append(content.get('text', '') if content.get('text') else "(No content)")
        
        # Add links section
        if content.get('all_links'):
            output.append("")
            output.append("-" * 40)
            output.append("LINKS FOUND IN THIS DISCUSSION:")
            output.append("-" * 40)
            for link in content.get('file_links', []):
                output.append(f"  📄 FILE: {link.get('url', '')}")
            for link in content.get('video_links', []):
                output.append(f"  🎥 VIDEO: {link.get('url', '')}")
            for link in content.get('external_links', []):
                output.append(f"  🔗 EXTERNAL: {link.get('url', '')}")
        
        filepath = dest_dir / f"{self.sanitize_filename(title)}.txt"
        with open(filepath, "w", encoding="utf-8") as f:
            f.write("\n".join(output))
        
        # Track with links
        self.tracker.mark_synced(SyncItem(
            item_id=f"discussion_{discussion_id}",
            item_type="discussion",
            title=discussion.get("title"),
            updated_at=updated_at,
            file_path=str(filepath.relative_to(DOWNLOAD_DIR)),
            links=content.get('all_links', [])
        ))
        
        print(f"      💬 {title}")
        self.stats["new"] += 1
        
        # Download linked files if course_id provided
        if course_id:
            for link in content.get('file_links', []):
                url = link.get('url', '')
                if not url:
                    continue
                    
                if url.startswith("/"):
                    url = f"{CANVAS_URL}{url}"
                
                # Ensure it's a download link
                if "/files/" in url and "/download" not in url:
                    url = url.rstrip("/") + "/download"
                
                # Extract file ID for tracking
                match = re.search(r'/files/(\d+)', url)
                file_id = match.group(1) if match else None
                
                filename = link.get('title', '').strip()
                if not filename or filename.lower() in ['here', 'click here', 'link', 'download']:
                    filename = f"linked_file_{file_id}" if file_id else "linked_file"
                
                await self.download_file(
                    url,
                    dest_dir / self.sanitize_filename(filename),
                    item_id=f"linked_file_{file_id}" if file_id else None,
                    title=filename,
                    course_id=course_id,
                    file_id=int(file_id) if file_id else None
                )
    
    async def sync_pages(self, course_id: int):
        """Sync standalone pages (not in modules)."""
        print("\n📄 Syncing standalone pages...")
        
        pages_dir = self.current_course_dir / "pages"
        pages_dir.mkdir(exist_ok=True)
        
        pages = await self.api_get_all(f"/courses/{course_id}/pages")
        
        if not pages:
            print("   No standalone pages")
            return
        
        synced = 0
        for page_summary in pages:
            page_url = page_summary.get("url")
            if page_url:
                page = await self.api_get(f"/courses/{course_id}/pages/{page_url}")
                if page:
                    title = self.sanitize_filename(page.get("title", page_url))
                    await self.save_page_with_links(page, pages_dir, title, course_id)
                    synced += 1
        
        print(f"   Synced {synced} pages")
    
    async def sync_assignments(self, course_id: int):
        """Sync standalone assignments."""
        print("\n📝 Syncing assignments...")
        
        assignments_dir = self.current_course_dir / "assignments"
        assignments_dir.mkdir(exist_ok=True)
        
        assignments = await self.api_get_all(f"/courses/{course_id}/assignments")
        
        if not assignments:
            print("   No assignments")
            return
        
        for assignment in assignments:
            title = self.sanitize_filename(assignment.get("name", "untitled"))
            await self.save_assignment(assignment, assignments_dir, title, course_id)
    
    async def sync_syllabus(self, course_id: int, course_name: str):
        """Sync course syllabus."""
        print("\n📋 Syncing syllabus...")
        
        course = await self.api_get(f"/courses/{course_id}", {"include[]": ["syllabus_body"]})
        
        if not course or not course.get("syllabus_body"):
            print("   No syllabus")
            return
        
        content = extract_content(course.get("syllabus_body", ""))
        
        output = []
        output.append("=" * 60)
        output.append(f"SYLLABUS: {course_name.upper()}")
        output.append("=" * 60)
        output.append("")
        output.append(content['text'])
        
        if content['all_links']:
            output.append("")
            output.append("-" * 40)
            output.append("LINKS:")
            for link in content['all_links']:
                output.append(f"  • {link['url']}")
        
        filepath = self.current_course_dir / "syllabus.txt"
        with open(filepath, "w", encoding="utf-8") as f:
            f.write("\n".join(output))
        
        self.tracker.mark_synced(SyncItem(
            item_id="syllabus",
            item_type="syllabus",
            title="Course Syllabus",
            updated_at=course.get("updated_at", ""),
            file_path=str(filepath.relative_to(DOWNLOAD_DIR)),
            links=content['all_links']
        ))
        
        print("   ✅ Syllabus saved")
    
    async def sync_announcements(self, course_id: int):
        """Sync course announcements."""
        print("\n📢 Syncing announcements...")
        
        announcements_dir = self.current_course_dir / "announcements"
        announcements_dir.mkdir(exist_ok=True)
        
        announcements = await self.api_get_all(
            "/announcements",
            {"context_codes[]": f"course_{course_id}"}
        )
        
        if not announcements:
            print("   No announcements")
            return
        
        for ann in announcements:
            title = self.sanitize_filename(ann.get("title", "untitled"))
            message = ann.get("message", "") or ""
            content = extract_content(message) if message else {'text': '', 'all_links': []}
            
            output = []
            output.append("=" * 60)
            output.append(f"ANNOUNCEMENT: {ann.get('title', title).upper()}")
            output.append("=" * 60)
            output.append(f"Posted: {ann.get('posted_at', 'Unknown')}")
            output.append("")
            output.append(content['text'] if content['text'] else "(No content)")
            
            filepath = announcements_dir / f"{title}.txt"
            with open(filepath, "w", encoding="utf-8") as f:
                f.write("\n".join(output))
            
            self.tracker.mark_synced(SyncItem(
                item_id=f"announcement_{ann['id']}",
                item_type="announcement",
                title=ann.get("title"),
                updated_at=ann.get("posted_at", ""),
                file_path=str(filepath.relative_to(DOWNLOAD_DIR))
            ))
        
        print(f"   Synced {len(announcements)} announcements")
    
    async def sync_quizzes(self, course_id: int):
        """Sync standalone quizzes."""
        print("\n❓ Syncing quizzes...")
        
        quizzes_dir = self.current_course_dir / "quizzes"
        quizzes_dir.mkdir(exist_ok=True)
        
        quizzes = await self.api_get_all(f"/courses/{course_id}/quizzes")
        
        if not quizzes:
            print("   No quizzes")
            return
        
        for quiz in quizzes:
            title = self.sanitize_filename(quiz.get("title", "untitled"))
            await self.save_quiz(quiz, quizzes_dir, title, course_id)
    
    async def sync_root_files(self, course_id: int):
        """Sync files from root folder (not already in modules)."""
        print("\n📁 Syncing root folder files...")
        
        files_dir = self.current_course_dir / "files"
        files_dir.mkdir(exist_ok=True)
        
        try:
            folders = await self.api_get(f"/courses/{course_id}/folders/root")
            if folders:
                root_id = folders.get("id")
                files = await self.api_get(f"/folders/{root_id}/files") or []
                
                if files:
                    print(f"   Found {len(files)} root files")
                    for file_info in files[:100]:  # Limit
                        url = file_info.get("url")
                        filename = self.sanitize_filename(
                            file_info.get("display_name") or file_info.get("filename", "unknown")
                        )
                        await self.download_file(
                            url,
                            files_dir / filename,
                            item_id=f"file_{file_info['id']}",
                            updated_at=file_info.get("updated_at"),
                            title=filename,
                            course_id=course_id,
                            file_id=file_info.get('id')
                        )
                else:
                    print("   No root files")
        except Exception as e:
            print(f"   ⚠️ Skipping root files: {e}")
    
    async def create_manifest(self, course: dict):
        """Create a manifest file for other tools to use."""
        stats = self.tracker.get_stats()
        
        manifest = {
            "version": "3.0",
            "generator": "canvas_sync.py",
            "course": {
                "id": str(course["id"]),
                "name": course.get("name"),
                "code": course.get("course_code"),
                "url": f"{CANVAS_URL}/courses/{course['id']}"
            },
            "synced_at": datetime.now().isoformat(),
            "stats": stats,
            "items": list(self.tracker.state["items"].values())
        }
        
        with open(self.current_course_dir / "_manifest.json", "w") as f:
            json.dump(manifest, f, indent=2, default=str)
        
        # Create comprehensive links summary (JSON and human-readable)
        all_links = []
        links_by_type = {
            "files": [],
            "videos": [],
            "external": [],
            "other": []
        }
        
        for item in self.tracker.state["items"].values():
            if item.get("links"):
                for link in item["links"]:
                    link_entry = {
                        "source_type": item.get("item_type", "unknown"),
                        "source_title": item.get("title", "Unknown"),
                        "source_path": item.get("file_path", ""),
                        "url": link.get("url", ""),
                        "title": link.get("title", ""),
                        "type": link.get("type", "unknown")
                    }
                    all_links.append(link_entry)
                    
                    # Categorize
                    url = link.get("url", "").lower()
                    if "/files/" in url or "file" in link.get("type", "").lower():
                        links_by_type["files"].append(link_entry)
                    elif any(v in url for v in ["youtube", "vimeo", "kaltura", "panopto", "video", "media"]):
                        links_by_type["videos"].append(link_entry)
                    elif url.startswith("http") and "canvas.santarosa.edu" not in url:
                        links_by_type["external"].append(link_entry)
                    else:
                        links_by_type["other"].append(link_entry)
        
        # Save JSON version
        if all_links:
            with open(self.current_course_dir / "_all_links.json", "w") as f:
                json.dump({
                    "total_links": len(all_links),
                    "by_type": {
                        "files": len(links_by_type["files"]),
                        "videos": len(links_by_type["videos"]),
                        "external": len(links_by_type["external"]),
                        "other": len(links_by_type["other"])
                    },
                    "links": all_links
                }, f, indent=2)
            
            # Save human-readable version
            with open(self.current_course_dir / "_all_links.txt", "w", encoding="utf-8") as f:
                f.write("=" * 80 + "\n")
                f.write(f"ALL LINKS FROM: {course.get('name', 'Course')}\n")
                f.write("=" * 80 + "\n")
                f.write(f"Total links: {len(all_links)}\n")
                f.write(f"  - Files: {len(links_by_type['files'])}\n")
                f.write(f"  - Videos: {len(links_by_type['videos'])}\n")
                f.write(f"  - External: {len(links_by_type['external'])}\n")
                f.write(f"  - Other: {len(links_by_type['other'])}\n")
                f.write("\n" + "=" * 80 + "\n\n")
                
                # Files section
                if links_by_type["files"]:
                    f.write("FILE LINKS:\n")
                    f.write("-" * 80 + "\n")
                    for link in links_by_type["files"]:
                        f.write(f"📄 {link['url']}\n")
                        f.write(f"   From: {link['source_title']} ({link['source_type']})\n\n")
                    f.write("\n")
                
                # Videos section
                if links_by_type["videos"]:
                    f.write("VIDEO LINKS:\n")
                    f.write("-" * 80 + "\n")
                    for link in links_by_type["videos"]:
                        f.write(f"🎥 {link['url']}\n")
                        f.write(f"   From: {link['source_title']} ({link['source_type']})\n\n")
                    f.write("\n")
                
                # External links section
                if links_by_type["external"]:
                    f.write("EXTERNAL LINKS:\n")
                    f.write("-" * 80 + "\n")
                    for link in links_by_type["external"]:
                        f.write(f"🔗 {link['url']}\n")
                        f.write(f"   From: {link['source_title']} ({link['source_type']})\n\n")
                    f.write("\n")
                
                # Other links section
                if links_by_type["other"]:
                    f.write("OTHER LINKS:\n")
                    f.write("-" * 80 + "\n")
                    for link in links_by_type["other"]:
                        f.write(f"🔸 {link['url']}\n")
                        f.write(f"   From: {link['source_title']} ({link['source_type']})\n\n")
            
            print(f"   📋 Saved {len(all_links)} links to _all_links.json and _all_links.txt")
    
    def sanitize_filename(self, name: str) -> str:
        """Make string safe for filename."""
        name = re.sub(r'[<>:"/\\|?*]', '_', str(name))
        name = re.sub(r'\s+', ' ', name)
        name = name.strip('. ')
        return name[:100]
    
    async def run(self):
        """Main entry point."""
        mode = "FORCE SYNC" if self.force_sync else "INCREMENTAL SYNC"
        
        print(f"""
╔═══════════════════════════════════════════════════════════╗
║         Canvas Content Sync ({mode})          ║
╚═══════════════════════════════════════════════════════════╝
        """)
        
        if not self.force_sync:
            print("💡 Use --force to re-download all content")
        
        print(f"📂 Download directory: {DOWNLOAD_DIR}")
        
        if not self.load_session():
            return
        
        courses = await self.get_courses()
        
        if not courses:
            print("❌ No courses found!")
            return
        
        for course in courses:
            await self.sync_course(course)
        
        print(f"\n{'='*60}")
        print("✅ Sync complete!")
        print(f"📂 Content saved to: {DOWNLOAD_DIR}")


async def main():
    import sys
    
    force_sync = "--force" in sys.argv or "-f" in sys.argv
    
    # Check for course filter
    course_filter = None
    for i, arg in enumerate(sys.argv):
        if arg in ["--course", "-c"] and i + 1 < len(sys.argv):
            course_filter = sys.argv[i + 1]
    
    syncer = CanvasSync(force_sync=force_sync, course_filter=course_filter)
    await syncer.run()


if __name__ == "__main__":
    asyncio.run(main())
