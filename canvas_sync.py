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
from datetime import datetime, date, timedelta, timezone
from urllib.parse import urljoin, unquote, urlparse, parse_qs
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
ZOOM_LTI_ADVANTAGE_URL = os.getenv("ZOOM_LTI_ADVANTAGE_URL", "https://applications.zoom.us/lti/advantage")

def normalize_url(url: str) -> str:
    """Normalize a URL from Canvas HTML to an absolute URL when possible."""
    if not url:
        return url
    url = str(url).strip()
    if url.startswith("//"):
        return f"https:{url}"
    if url.startswith("/"):
        return f"{CANVAS_URL}{url}"
    return url


def unwrap_canvas_deep_link(url: str) -> Optional[str]:
    """Best-effort unwrap of Canvas redirect/launch URLs to their external target.

    Canvas often wraps external resources in URLs like:
    - /courses/.../external_tools/retrieve?url=<encoded>
    - /courses/.../external_url?url=<encoded>
    This tries to extract and decode those embedded targets.
    """
    if not url:
        return None

    # Make absolute so urlparse has host info for relative Canvas links.
    url_abs = normalize_url(url)

    try:
        parsed = urlparse(url_abs)
    except Exception:
        return None

    host = (parsed.hostname or "").lower()
    if host and host != urlparse(CANVAS_URL).hostname:
        # Already external.
        return None

    qs = parse_qs(parsed.query or "")
    # Common parameter names used to carry external targets.
    candidate_params = ["url", "redirect", "redirect_uri", "return_to", "next", "target"]
    candidates: list[str] = []
    for k in candidate_params:
        for v in qs.get(k, []):
            if v:
                candidates.append(v)

    def decode_maybe_twice(s: str) -> str:
        # Canvas links are often percent-encoded once (sometimes nested).
        s1 = unquote(s)
        s2 = unquote(s1)
        return s2

    for cand in candidates:
        decoded = decode_maybe_twice(cand).strip()
        if not decoded:
            continue
        if decoded.startswith("/"):
            decoded = f"{CANVAS_URL}{decoded}"
        if decoded.startswith("http"):
            try:
                decoded_host = (urlparse(decoded).hostname or "").lower()
                if decoded_host and decoded_host != urlparse(CANVAS_URL).hostname:
                    return decoded
            except Exception:
                # If it looks like a URL but parsing fails, still prefer it.
                return decoded
    return None


def is_zoom_url(url: str) -> bool:
    """Best-effort detection for direct Zoom domains (join links, LTI, etc.)."""
    if not url:
        return False
    try:
        parsed = urlparse(url if "://" in url else f"https://{url.lstrip('/')}")
        host = (parsed.hostname or "").lower()
        return host.endswith("zoom.us") or host.endswith("zoomgov.com")
    except Exception:
        lower = str(url).lower()
        return ("zoom.us" in lower) or ("zoomgov.com" in lower)


def is_zoom_related(url: str, title: str = "", source_title: str = "") -> bool:
    """Detect Zoom even when the link is a Canvas LTI launch."""
    if is_zoom_url(url):
        return True
    haystack = f"{url} {title} {source_title}".lower()
    return "zoom" in haystack

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
        self.internal_links = []  # Canvas/internal URLs (pages, modules, etc.)
        self.current_link = None
        self.current_link_attrs = {}
        self.current_link_text_parts = []
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
                self.current_link_attrs = attrs_dict
                self.current_link_text_parts = []
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
    
    def _categorize_link(self, href: str, title: str = '', text: str = '') -> dict:
        """Categorize a link by type."""
        href_abs = normalize_url(href)
        resolved = unwrap_canvas_deep_link(href_abs) or href_abs
        link_info = {
            'url': href_abs,
            'resolved_url': resolved,
            'title': title,
            'text': text
        }
        
        # Canvas file links
        if '/files/' in resolved:
            self.file_links.append(link_info)
        # Video platforms
        elif any(v in resolved for v in ['youtube.com', 'youtu.be', 'vimeo.com', 'kaltura', 'panopto']):
            self.video_links.append({**link_info, 'type': 'external_video'})
        # Media objects
        elif '/media_objects/' in resolved or '/media/' in resolved:
            self.video_links.append({**link_info, 'type': 'canvas_media'})
        # External links
        elif resolved.startswith('mailto:') or (resolved.startswith('http') and 'canvas.santarosa.edu' not in resolved):
            self.external_links.append(link_info)
        # Internal Canvas links
        elif resolved.startswith('http') and 'canvas.santarosa.edu' in resolved:
            self.internal_links.append(link_info)
        
        self.links.append(link_info)
        return link_info
    
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
            if self.current_link:
                title = (
                    self.current_link_attrs.get('title')
                    or self.current_link_attrs.get('aria-label')
                    or ''
                )
                text = ' '.join(self.current_link_text_parts).strip()
                link_info = self._categorize_link(self.current_link, title=title, text=text)

                display = (text or title or (link_info.get('resolved_url') or link_info.get('url') or '')).strip()
                url = (link_info.get('resolved_url') or link_info.get('url') or '').strip()
                if url:
                    if display and display != url:
                        self.text_parts.append(f"{display} ({url})")
                    else:
                        self.text_parts.append(url)

            self.current_link = None
            self.current_link_attrs = {}
            self.current_link_text_parts = []
    
    def handle_data(self, data):
        if self.in_script or self.in_style:
            return
        text = data.strip()
        if text:
            if self.current_link is not None:
                self.current_link_text_parts.append(text)
            else:
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
            'external_links': parser.external_links or [],
            'internal_links': parser.internal_links or []
        }
    except Exception:
        # Fallback - always return all keys
        text = re.sub(r'<[^>]+>', ' ', html)
        return {
            'text': text.strip(), 
            'all_links': [], 
            'file_links': [], 
            'video_links': [], 
            'external_links': [],
            'internal_links': []
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
    # Scheduling / context (used for weekly bundling)
    due_at: Optional[str] = None
    unlock_at: Optional[str] = None
    module_id: Optional[str] = None
    module_name: Optional[str] = None
    module_unlock_at: Optional[str] = None


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
    
    def __init__(self, force_sync: bool = False, course_filter: str = None, bundle_weeks: bool = False):
        self.force_sync = force_sync
        self.course_filter = course_filter.lower() if course_filter else None
        # Also keep a whitespace-stripped variant so "FDNT 10" matches "FDNT10"
        self.course_filter_compact = re.sub(r"\s+", "", self.course_filter) if self.course_filter else None
        self.bundle_weeks = bundle_weeks
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

    def unwrap_canvas_deep_link(self, url: str) -> Optional[str]:
        """Best-effort unwrap of Canvas redirect/launch URLs to their external target.

        Canvas often wraps external resources in URLs like:
        - /courses/.../external_tools/retrieve?url=<encoded>
        - /courses/.../external_url?url=<encoded>
        This tries to extract and decode those embedded targets.
        """
        if not url:
            return None

        # Make absolute so urlparse has host info for relative Canvas links.
        url_abs = url
        if url.startswith("/"):
            url_abs = f"{CANVAS_URL}{url}"

        try:
            parsed = urlparse(url_abs)
        except Exception:
            return None

        host = (parsed.hostname or "").lower()
        if host and host != urlparse(CANVAS_URL).hostname:
            # Already external.
            return None

        qs = parse_qs(parsed.query or "")
        # Common parameter names used to carry external targets.
        candidate_params = ["url", "redirect", "redirect_uri", "return_to", "next", "target"]
        candidates: list[str] = []
        for k in candidate_params:
            for v in qs.get(k, []):
                if v:
                    candidates.append(v)

        def decode_maybe_twice(s: str) -> str:
            # Canvas links are often percent-encoded once (sometimes nested).
            s1 = unquote(s)
            s2 = unquote(s1)
            return s2

        for cand in candidates:
            decoded = decode_maybe_twice(cand).strip()
            if not decoded:
                continue
            if decoded.startswith("/"):
                decoded = f"{CANVAS_URL}{decoded}"
            if decoded.startswith("http"):
                try:
                    decoded_host = (urlparse(decoded).hostname or "").lower()
                    if decoded_host and decoded_host != urlparse(CANVAS_URL).hostname:
                        return decoded
                except Exception:
                    # If it looks like a URL but parsing fails, still prefer it.
                    return decoded
        return None

    def _unique_url_shortcut_path(self, desired_path: Path, *, discriminator: str) -> Path:
        """Avoid collisions for .url shortcuts (titles are often duplicated/truncated).

        If the desired path already exists, create a stable unique variant by appending
        the discriminator (usually the Canvas module item ID).
        """
        if not desired_path.exists():
            return desired_path
        # If already has discriminator, keep it.
        if discriminator and discriminator in desired_path.stem:
            return desired_path
        return desired_path.with_name(f"{desired_path.stem}_{discriminator}{desired_path.suffix}")
    
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
    
    async def download_file(
        self,
        url: str,
        dest_path: Path,
        item_id: str = None,
        updated_at: str = None,
        title: str = None,
        course_id: int = None,
        file_id: int = None,
        module_id: Optional[int] = None,
        module_name: Optional[str] = None,
        module_unlock_at: Optional[str] = None,
    ) -> bool:
        """Download a file with tracking."""
        try:
            # Skip if already synced and not changed
            if item_id and not self.force_sync:
                if not self.tracker.needs_sync(item_id, updated_at or "", dest_path):
                    self.stats["skipped"] += 1
                    return True
            
            async with httpx.AsyncClient(
                cookies=self.cookies,
                follow_redirects=True,
                timeout=300,
            ) as client:
                async with client.stream("GET", url) as resp:
                    if resp.status_code != 200:
                        return False

                    # Get actual filename from Content-Disposition if available
                    cd = resp.headers.get("content-disposition", "")
                    if "filename=" in cd:
                        match = re.search(
                            r'filename\*?=["\']?(?:UTF-8\'\')?([^";\n\r\']+)',
                            cd,
                            re.IGNORECASE,
                        )
                        if match:
                            actual_name = unquote(match.group(1))
                            dest_path = dest_path.parent / self.sanitize_filename(actual_name)

                    dest_path.parent.mkdir(parents=True, exist_ok=True)
                    bytes_written = 0
                    with open(dest_path, "wb") as f:
                        async for chunk in resp.aiter_bytes():
                            if not chunk:
                                continue
                            f.write(chunk)
                            bytes_written += len(chunk)

                    # Check if it's a PowerPoint file - add Canvas page link for inline videos
                    is_powerpoint = dest_path.suffix.lower() in [".ppt", ".pptx"]
                    canvas_file_url = None
                    if is_powerpoint and course_id and file_id:
                        canvas_file_url = f"{CANVAS_URL}/courses/{course_id}/files/{file_id}"
                        # Create a companion file with the Canvas link
                        link_file = dest_path.with_suffix(dest_path.suffix + ".canvas_link.txt")
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
                        tracked_links.append(
                            {
                                "url": canvas_file_url,
                                "title": "Canvas File Page (for inline videos)",
                                "type": "canvas_file_page",
                            }
                        )

                    if item_id:
                        self.tracker.mark_synced(
                            SyncItem(
                                item_id=item_id,
                                item_type="file",
                                title=title or dest_path.name,
                                updated_at=updated_at or "",
                                file_path=str(dest_path.relative_to(DOWNLOAD_DIR)),
                                source_url=canvas_file_url or url,
                                file_size=bytes_written,
                                module_id=str(module_id) if module_id is not None else None,
                                module_name=module_name,
                                module_unlock_at=module_unlock_at,
                                links=tracked_links if tracked_links else None,
                            )
                        )
                        self.stats["new"] += 1

                    if is_powerpoint:
                        print(f"      ✅ {dest_path.name} (PowerPoint - Canvas link saved)")
                    else:
                        print(f"      ✅ {dest_path.name}")
                    return True
                    
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
            if self.course_filter:
                name_lower = name.lower()
                name_compact = re.sub(r"\s+", "", name_lower)
                if (self.course_filter not in name_lower) and (
                    self.course_filter_compact and (self.course_filter_compact not in name_compact)
                ):
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
                unlock_at=unlock_at,
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
                await self.sync_module_item(
                    course_id,
                    item,
                    module_dir,
                    module_name,
                    module_id=module_id,
                    module_unlock_at=unlock_at,
                )
    
    async def sync_module_item(
        self,
        course_id: int,
        item: dict,
        module_dir: Path,
        module_name: str,
        *,
        module_id: Optional[int] = None,
        module_unlock_at: Optional[str] = None,
    ):
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
                        file_id=content_id,
                        module_id=module_id,
                        module_name=module_name,
                        module_unlock_at=module_unlock_at,
                    )
        
        elif item_type == "page":
            # Wiki page - save content and extract all links
            page_url = item.get("page_url")
            if page_url:
                page = await self.api_get(f"/courses/{course_id}/pages/{page_url}")
                if page:
                    await self.save_page_with_links(
                        page,
                        module_dir,
                        title,
                        course_id,
                        module_id=module_id,
                        module_name=module_name,
                        module_unlock_at=module_unlock_at,
                    )
        
        elif item_type == "assignment":
            # Assignment details
            if content_id:
                assignment = await self.api_get(f"/courses/{course_id}/assignments/{content_id}")
                if assignment:
                    await self.save_assignment(
                        assignment,
                        module_dir,
                        title,
                        course_id,
                        module_id=module_id,
                        module_name=module_name,
                        module_unlock_at=module_unlock_at,
                    )
        
        elif item_type == "quiz":
            # Quiz in module
            if content_id:
                quiz = await self.api_get(f"/courses/{course_id}/quizzes/{content_id}")
                if quiz:
                    await self.save_quiz(
                        quiz,
                        module_dir,
                        title,
                        course_id,
                        module_id=module_id,
                        module_name=module_name,
                        module_unlock_at=module_unlock_at,
                    )
        
        elif item_type == "discussion":
            # Discussion topic
            if content_id:
                discussion = await self.api_get(f"/courses/{course_id}/discussion_topics/{content_id}")
                if discussion:
                    await self.save_discussion(discussion, module_dir, title, course_id)
        
        elif item_type == "externalurl":
            # External URL - save as .url file and track link
            raw_url = item.get("external_url", "")
            if raw_url:
                resolved = self.unwrap_canvas_deep_link(raw_url) or raw_url
                url_file = module_dir / f"{self.sanitize_filename(title)}.url"
                url_file = self._unique_url_shortcut_path(url_file, discriminator=str(item_id))
                link_txt = module_dir / f"{self.sanitize_filename(title)}.link.txt"
                link_txt = self._unique_url_shortcut_path(link_txt, discriminator=str(item_id))
                
                # Check if needs sync
                if not self.force_sync:
                    version = resolved if resolved == raw_url else f"{raw_url} -> {resolved}"
                    if not self.tracker.needs_sync(f"exturl_{item_id}", version, url_file):
                        self.stats["skipped"] += 1
                        return
                
                with open(url_file, "w") as f:
                    f.write(f"[InternetShortcut]\nURL={resolved}\n")

                # Also write a plain-text link file (clickable in Google Drive web preview).
                with open(link_txt, "w", encoding="utf-8") as f:
                    f.write(f"{title}\n")
                    f.write("=" * 60 + "\n\n")
                    f.write(f"{resolved}\n")
                    if resolved != raw_url:
                        f.write("\nRaw Canvas URL (wrapper):\n")
                        f.write(f"{raw_url}\n")
                
                # Track with link info
                self.tracker.mark_synced(SyncItem(
                    item_id=f"exturl_{item_id}",
                    item_type="external_url",
                    title=title,
                    updated_at=resolved if resolved == raw_url else f"{raw_url} -> {resolved}",
                    file_path=str(url_file.relative_to(DOWNLOAD_DIR)),
                    source_url=resolved,
                    module_id=str(module_id) if module_id is not None else None,
                    module_name=module_name,
                    module_unlock_at=module_unlock_at,
                    links=[{"url": resolved, "title": title, "type": "external_url"}]
                ))
                print(f"      🔗 {title}")
                self.stats["new"] += 1
        
        elif item_type == "externaltool":
            # External tool (might be video embed, LTI content, etc.)
            raw_url = item.get("url") or item.get("external_url", "")
            if raw_url:
                # Prefer the embedded external target if Canvas wrapped it.
                resolved = self.unwrap_canvas_deep_link(raw_url) or raw_url
                url_file = module_dir / f"{self.sanitize_filename(title)}_tool.url"
                url_file = self._unique_url_shortcut_path(url_file, discriminator=str(item_id))
                link_txt = module_dir / f"{self.sanitize_filename(title)}_tool.link.txt"
                link_txt = self._unique_url_shortcut_path(link_txt, discriminator=str(item_id))
                
                # Check if needs sync
                if not self.force_sync:
                    version = resolved if resolved == raw_url else f"{raw_url} -> {resolved}"
                    if not self.tracker.needs_sync(f"exttool_{item_id}", version, url_file):
                        self.stats["skipped"] += 1
                        return
                
                # Save the link
                with open(url_file, "w") as f:
                    f.write(f"[InternetShortcut]\nURL={resolved}\n")

                # Also write a plain-text link file (clickable in Google Drive web preview).
                with open(link_txt, "w", encoding="utf-8") as f:
                    f.write(f"{title}\n")
                    f.write("=" * 60 + "\n\n")
                    f.write(f"{resolved}\n")
                    if resolved != raw_url:
                        f.write("\nRaw Canvas URL (wrapper):\n")
                        f.write(f"{raw_url}\n")
                
                # Track with link info
                self.tracker.mark_synced(SyncItem(
                    item_id=f"exttool_{item_id}",
                    item_type="external_tool",
                    title=title,
                    updated_at=resolved if resolved == raw_url else f"{raw_url} -> {resolved}",
                    file_path=str(url_file.relative_to(DOWNLOAD_DIR)),
                    source_url=resolved,
                    module_id=str(module_id) if module_id is not None else None,
                    module_name=module_name,
                    module_unlock_at=module_unlock_at,
                    links=[{"url": resolved, "title": title, "type": "external_tool"}]
                ))
                print(f"      🔧 {title} (external tool)")
                self.stats["new"] += 1
        
        elif item_type == "subheader":
            # Just a section header, skip
            pass
    
    async def save_page_with_links(
        self,
        page: dict,
        dest_dir: Path,
        title: str,
        course_id: int,
        *,
        module_id: Optional[int] = None,
        module_name: Optional[str] = None,
        module_unlock_at: Optional[str] = None,
    ):
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
        content.setdefault('internal_links', [])
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
            
            for link in content.get('internal_links', []):
                url = link.get('resolved_url') or link.get('url', '')
                label = (link.get('text') or link.get('title') or '').strip()
                output.append(f"  🧭 CANVAS: {label + ' - ' if label else ''}{url}")
            for link in content.get('file_links', []):
                url = link.get('resolved_url') or link.get('url', '')
                label = (link.get('text') or link.get('title') or '').strip()
                output.append(f"  📄 FILE: {label + ' - ' if label else ''}{url}")
            for link in content.get('video_links', []):
                url = link.get('resolved_url') or link.get('url', '')
                label = (link.get('text') or link.get('title') or '').strip()
                output.append(f"  🎥 VIDEO: {label + ' - ' if label else ''}{url}")
            for link in content.get('external_links', []):
                url = link.get('resolved_url') or link.get('url', '')
                label = (link.get('text') or link.get('title') or '').strip()
                output.append(f"  🔗 EXTERNAL: {label + ' - ' if label else ''}{url}")
        
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
            module_id=str(module_id) if module_id is not None else None,
            module_name=module_name,
            module_unlock_at=module_unlock_at,
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
    
    async def save_assignment(
        self,
        assignment: dict,
        dest_dir: Path,
        title: str,
        course_id: int = None,
        *,
        module_id: Optional[int] = None,
        module_name: Optional[str] = None,
        module_unlock_at: Optional[str] = None,
    ):
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
        content.setdefault('internal_links', [])
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
            for link in content.get('internal_links', []):
                url = link.get('resolved_url') or link.get('url', '')
                label = (link.get('text') or link.get('title') or '').strip()
                output.append(f"  🧭 CANVAS: {label + ' - ' if label else ''}{url}")
            for link in content.get('file_links', []):
                url = link.get('resolved_url') or link.get('url', '')
                label = (link.get('text') or link.get('title') or '').strip()
                output.append(f"  📄 FILE: {label + ' - ' if label else ''}{url}")
            for link in content.get('video_links', []):
                url = link.get('resolved_url') or link.get('url', '')
                label = (link.get('text') or link.get('title') or '').strip()
                output.append(f"  🎥 VIDEO: {label + ' - ' if label else ''}{url}")
            for link in content.get('external_links', []):
                url = link.get('resolved_url') or link.get('url', '')
                label = (link.get('text') or link.get('title') or '').strip()
                output.append(f"  🔗 EXTERNAL: {label + ' - ' if label else ''}{url}")
        
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
            due_at=assignment.get("due_at"),
            module_id=str(module_id) if module_id is not None else None,
            module_name=module_name,
            module_unlock_at=module_unlock_at,
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
    
    async def save_quiz(
        self,
        quiz: dict,
        dest_dir: Path,
        title: str,
        course_id: int,
        *,
        module_id: Optional[int] = None,
        module_name: Optional[str] = None,
        module_unlock_at: Optional[str] = None,
    ):
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
        content.setdefault('internal_links', [])
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
            for link in all_links:
                url = (link.get('resolved_url') or link.get('url') or '').strip()
                if not url:
                    continue
                label = (link.get('text') or link.get('title') or '').strip()
                if label and label != url:
                    output.append(f"  • {label} - {url}")
                else:
                    output.append(f"  • {url}")
        
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
            due_at=quiz.get("due_at"),
            module_id=str(module_id) if module_id is not None else None,
            module_name=module_name,
            module_unlock_at=module_unlock_at,
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
        content.setdefault('internal_links', [])
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
            for link in content.get('internal_links', []):
                url = link.get('resolved_url') or link.get('url', '')
                label = (link.get('text') or link.get('title') or '').strip()
                output.append(f"  🧭 CANVAS: {label + ' - ' if label else ''}{url}")
            for link in content.get('file_links', []):
                url = link.get('resolved_url') or link.get('url', '')
                label = (link.get('text') or link.get('title') or '').strip()
                output.append(f"  📄 FILE: {label + ' - ' if label else ''}{url}")
            for link in content.get('video_links', []):
                url = link.get('resolved_url') or link.get('url', '')
                label = (link.get('text') or link.get('title') or '').strip()
                output.append(f"  🎥 VIDEO: {label + ' - ' if label else ''}{url}")
            for link in content.get('external_links', []):
                url = link.get('resolved_url') or link.get('url', '')
                label = (link.get('text') or link.get('title') or '').strip()
                output.append(f"  🔗 EXTERNAL: {label + ' - ' if label else ''}{url}")
        
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
                url = (link.get('resolved_url') or link.get('url') or '').strip()
                if not url:
                    continue
                label = (link.get('text') or link.get('title') or '').strip()
                if label and label != url:
                    output.append(f"  • {label} - {url}")
                else:
                    output.append(f"  • {url}")
        
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
            "zoom": [],
            "external": [],
            "other": []
        }
        
        for item in self.tracker.state["items"].values():
            if item.get("links"):
                for link in item["links"]:
                    url_raw = link.get("url", "")
                    link_entry = {
                        "source_type": item.get("item_type", "unknown"),
                        "source_title": item.get("title", "Unknown"),
                        "source_path": item.get("file_path", ""),
                        "url": url_raw,
                        "title": link.get("title", ""),
                        "type": link.get("type", "unknown")
                    }
                    all_links.append(link_entry)
                    
                    # Categorize
                    url_lower = url_raw.lower()
                    if is_zoom_related(url_raw, link_entry.get("title", ""), link_entry.get("source_title", "")):
                        links_by_type["zoom"].append(link_entry)
                    elif "/files/" in url_lower or "file" in link.get("type", "").lower():
                        links_by_type["files"].append(link_entry)
                    elif any(v in url_lower for v in ["youtube", "vimeo", "kaltura", "panopto", "video", "media"]):
                        links_by_type["videos"].append(link_entry)
                    elif url_lower.startswith("http") and "canvas.santarosa.edu" not in url_lower:
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
                        "zoom": len(links_by_type["zoom"]),
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
                f.write(f"  - Zoom: {len(links_by_type['zoom'])}\n")
                f.write(f"  - External: {len(links_by_type['external'])}\n")
                f.write(f"  - Other: {len(links_by_type['other'])}\n")
                f.write("\n" + "=" * 80 + "\n\n")
                
                # Zoom section
                if links_by_type["zoom"]:
                    f.write("ZOOM LINKS:\n")
                    f.write("-" * 80 + "\n")
                    for link in links_by_type["zoom"]:
                        f.write(f"🎦 {link['url']}\n")
                        f.write(f"   From: {link['source_title']} ({link['source_type']})\n\n")
                    f.write("\n")

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

            # Save dedicated Zoom link list (useful for adding to calendars later)
            if links_by_type["zoom"]:
                seen_urls = set()
                unique_zoom = []
                for entry in links_by_type["zoom"]:
                    u = (entry.get("url") or "").strip()
                    if not u or u in seen_urls:
                        continue
                    seen_urls.add(u)
                    unique_zoom.append(entry)

                # Always include the Zoom LTI "advantage" portal as a fallback entry point
                if ZOOM_LTI_ADVANTAGE_URL not in seen_urls:
                    unique_zoom.insert(0, {
                        "source_type": "meta",
                        "source_title": course.get("name", "Course"),
                        "source_path": "",
                        "url": ZOOM_LTI_ADVANTAGE_URL,
                        "title": "Zoom LTI (Advantage) portal",
                        "type": "zoom_portal"
                    })

                with open(self.current_course_dir / "_zoom_links.json", "w") as f:
                    json.dump({
                        "total_zoom_links": len(unique_zoom),
                        "links": unique_zoom
                    }, f, indent=2)

                with open(self.current_course_dir / "_zoom_links.txt", "w", encoding="utf-8") as f:
                    f.write("=" * 80 + "\n")
                    f.write(f"ZOOM LINKS FROM: {course.get('name', 'Course')}\n")
                    f.write("=" * 80 + "\n\n")
                    for link in unique_zoom:
                        title = (link.get("title") or "").strip()
                        from_title = (link.get("source_title") or "").strip()
                        from_type = (link.get("source_type") or "").strip()
                        if title:
                            f.write(f"{title}\n")
                        f.write(f"{link.get('url','')}\n")
                        if from_title or from_type:
                            f.write(f"From: {from_title} ({from_type})\n")
                        f.write("\n")
    
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

        if self.bundle_weeks:
            print("\n📅 Building weekly bundles...")
            try:
                bundle_weekly_exports(DOWNLOAD_DIR)
                print("   ✅ Weekly bundles updated")
            except Exception as e:
                print(f"   ⚠️ Weekly bundling failed: {e}")
        
        print(f"\n{'='*60}")
        print("✅ Sync complete!")
        print(f"📂 Content saved to: {DOWNLOAD_DIR}")


def _parse_canvas_datetime(value: Optional[str]) -> Optional[datetime]:
    """Parse Canvas ISO timestamps (usually UTC 'Z') into an aware datetime."""
    if not value:
        return None
    s = str(value).strip()
    if not s:
        return None
    # Canvas commonly uses Z for UTC.
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(s)
    except Exception:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _iso_week_key(dt_local: datetime) -> str:
    iso = dt_local.date().isocalendar()
    return f"{iso.year}-W{iso.week:02d}"


def _week_start_end_dates(week_key: str) -> tuple[date, date]:
    # week_key format: YYYY-Www
    try:
        year_str, week_str = week_key.split("-W", 1)
        iso_year = int(year_str)
        iso_week = int(week_str)
    except Exception as e:
        raise ValueError(f"Invalid week key: {week_key}") from e
    start = date.fromisocalendar(iso_year, iso_week, 1)  # Monday
    end = date.fromisocalendar(iso_year, iso_week, 7)    # Sunday
    return start, end


def _looks_like_reading(title: str) -> bool:
    t = title or ""
    # Heuristic: only mark as reading when the title explicitly signals it.
    return re.search(r"\b(read|reading|chapter|chapters|ch\.|pp\.)\b", t, flags=re.IGNORECASE) is not None


def _extract_reading_spec(title: str) -> dict:
    raw = title or ""
    chapters = re.findall(
        r"\bch(?:apter)?s?\.?\s*([0-9]+(?:\.[0-9]+)?(?:\s*[-–]\s*[0-9]+(?:\.[0-9]+)?)?)",
        raw,
        flags=re.IGNORECASE,
    )
    pages = re.findall(
        r"\bpp?\.?\s*([0-9]+(?:\s*[-–]\s*[0-9]+)?)",
        raw,
        flags=re.IGNORECASE,
    )
    return {
        "raw": raw,
        "chapters": chapters,
        "pages": pages,
    }


def _infer_due_at_from_text_file(download_dir: Path, local_relative_path: Optional[str]) -> Optional[str]:
    """Backward-compatible: infer Due: ... from older saved assignment/quiz .txt files."""
    if not local_relative_path:
        return None
    try:
        p = download_dir / local_relative_path
    except Exception:
        return None
    if not p.exists() or not p.is_file():
        return None
    try:
        with open(p, "r", encoding="utf-8", errors="ignore") as f:
            for _ in range(120):  # only need the header
                line = f.readline()
                if not line:
                    break
                m = re.match(r"^\s*Due:\s*(.+?)\s*$", line)
                if not m:
                    continue
                val = (m.group(1) or "").strip()
                if not val or val.lower().startswith("no due"):
                    return None
                return val
    except Exception:
        return None
    return None


def _infer_module_folder_from_relative_path(local_relative_path: Optional[str]) -> Optional[str]:
    """Infer the module folder name from a saved file path like: <course>/modules/<module>/..."""
    if not local_relative_path:
        return None
    try:
        parts = Path(str(local_relative_path)).parts
    except Exception:
        return None
    for i, p in enumerate(parts):
        if p == "modules" and i + 1 < len(parts):
            return parts[i + 1]
    return None


def _stable_canvas_url_for_item(*, course_id: str, item_type: str, item_id: str, source_url: Optional[str]) -> Optional[str]:
    """Return a stable Canvas URL when possible (file page, assignment page, etc.)."""
    course_id = str(course_id or "").strip()
    item_type = (item_type or "").strip().lower()
    item_id = str(item_id or "").strip()

    if not course_id:
        return source_url

    def suffix(prefix: str) -> Optional[str]:
        return item_id[len(prefix):] if item_id.startswith(prefix) and len(item_id) > len(prefix) else None

    if item_type == "assignment":
        aid = suffix("assignment_")
        return f"{CANVAS_URL}/courses/{course_id}/assignments/{aid}" if aid else source_url
    if item_type == "quiz":
        qid = suffix("quiz_")
        return f"{CANVAS_URL}/courses/{course_id}/quizzes/{qid}" if qid else source_url
    if item_type == "file":
        fid = suffix("file_") or suffix("linked_file_")
        return f"{CANVAS_URL}/courses/{course_id}/files/{fid}" if fid else source_url
    if item_type == "page":
        pid = suffix("page_")
        return f"{CANVAS_URL}/courses/{course_id}/pages/{pid}" if pid else source_url
    if item_type == "module":
        mid = suffix("module_")
        return f"{CANVAS_URL}/courses/{course_id}/modules/{mid}" if mid else source_url
    if item_type == "discussion":
        did = suffix("discussion_")
        return f"{CANVAS_URL}/courses/{course_id}/discussion_topics/{did}" if did else source_url

    return source_url


def _resource_kind_from_url(url: str, link_type: str = "") -> str:
    u = (url or "").lower()
    t = (link_type or "").lower()
    if "/files/" in u or "file" in t:
        return "file"
    if any(v in u for v in ["youtube.com", "youtu.be", "vimeo.com", "kaltura", "panopto", "media_objects", "/media/"]):
        return "video"
    if "ted.com/talks/" in u:
        return "video"
    if "canvas.santarosa.edu" in u:
        return "canvas"
    return "external"


def _sanitize_weekly_path_component(name: str, max_len: int = 80) -> str:
    """Safe folder/file component for weekly bundles."""
    s = re.sub(r'[<>:"/\\|?*\n\r\t]', "_", str(name or "")).strip()
    s = re.sub(r"\s+", " ", s).strip(" .")
    if len(s) > max_len:
        s = s[:max_len].rstrip(" .")
    return s or "untitled"


def _is_video_file_relpath(relpath: str) -> bool:
    p = str(relpath or "").lower()
    return any(p.endswith(ext) for ext in [".mp4", ".m4v", ".mov", ".webm", ".mkv"])


def _looks_like_recording(title: str, relpath: str = "") -> bool:
    h = f"{title} {relpath}".lower()
    return any(k in h for k in ["recording", "zoom recording", "class recording", "lecture recording", "session recording"])


def _summarize_prep_focus(materials: list[dict], zoom_links: list[dict]) -> list[str]:
    """Return short action phrases for a prep item title."""
    actions: list[str] = []
    if zoom_links:
        actions.append("watch Zoom recording")

    has_video = any((m.get("material_kind") == "video") or _is_video_file_relpath(m.get("local_relative_path", "")) for m in materials)
    if has_video and "watch Zoom recording" not in actions:
        actions.append("watch video")

    has_reading = any((m.get("resource_category") == "reading") or (m.get("material_kind") == "reading") for m in materials)
    if has_reading:
        actions.append("do reading")

    has_slides = any(re.search(r"\b(slides?|pptx?|powerpoint)\b", (m.get("title") or ""), flags=re.IGNORECASE) for m in materials)
    if has_slides:
        actions.append("review slides")

    return actions[:3] or ["review materials"]


def _priority_hint(kind: str, title: str) -> str:
    k = (kind or "").lower()
    t = (title or "").lower()
    if k in {"assignment", "quiz"}:
        # De-emphasize routine participation/attendance items vs learning/graded work.
        if any(w in t for w in ["participation", "attendance", "check-in", "check in"]):
            return "medium"
        return "high"
    if k == "prep":
        return "high"
    if k == "resource":
        return "low"
    return "medium"


def _read_local_text_snippet(download_dir: Path, relpath: str, *, max_chars: int = 40_000) -> str:
    if not relpath:
        return ""
    p = download_dir / relpath
    if not p.exists() or not p.is_file():
        return ""
    if p.suffix.lower() not in [".txt", ".md", ".link", ".link.txt"]:
        return ""
    try:
        data = p.read_text(encoding="utf-8", errors="ignore").strip()
    except Exception:
        return ""
    if len(data) > max_chars:
        return data[:max_chars].rstrip() + "\n\n(…truncated…)\n"
    return data


def _write_task_bundle_file(
    *,
    download_dir: Path,
    week_folder: Path,
    task: dict,
    materials: list[dict],
    zoom_links: list[dict],
) -> Optional[str]:
    """Write a self-contained task bundle markdown file and return relpath (from download_dir)."""
    course_name = ((task.get("course") or {}).get("name") or "course").strip()
    safe_course = _sanitize_weekly_path_component(course_name)
    safe_id = _sanitize_weekly_path_component(str(task.get("id") or task.get("title") or "task"))
    safe_title = _sanitize_weekly_path_component(str(task.get("title") or "task"), max_len=60)

    out_dir = week_folder / "tasks" / safe_course
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"{safe_title}__{safe_id}.md"

    lines: list[str] = []
    lines.append(f"# {task.get('title') or 'Task'}")
    lines.append("")
    lines.append(f"- Course: {course_name}")
    if task.get("scheduled_date_local"):
        lines.append(f"- Scheduled: {task.get('scheduled_date_local')}")
    if task.get("due_at"):
        lines.append(f"- Due (Canvas): {task.get('due_at')}")
    if task.get("direct_url"):
        lines.append(f"- Canvas link: {task.get('direct_url')}")
    lines.append("")

    # Instructions / primary content
    snippet = _read_local_text_snippet(download_dir, task.get("local_relative_path") or "")
    if snippet:
        lines.append("## Instructions / context")
        lines.append("")
        lines.append(snippet)
        if not snippet.endswith("\n"):
            lines.append("")

    if zoom_links:
        lines.append("## Zoom / class session")
        lines.append("")
        for z in zoom_links[:15]:
            zurl = (z.get("url") or "").strip()
            ztitle = (z.get("title") or z.get("text") or "").strip()
            if ztitle:
                lines.append(f"- {ztitle}: {zurl}")
            else:
                lines.append(f"- {zurl}")
        lines.append("")

    if materials:
        lines.append("## Materials (everything referenced / in-module)")
        lines.append("")
        for m in materials[:80]:
            title = (m.get("title") or "").strip() or "(untitled)"
            local_rel = (m.get("local_relative_path") or "").strip()
            url = (m.get("direct_url") or m.get("url") or "").strip()
            kind = (m.get("material_kind") or m.get("resource_type") or m.get("resource_type") or "").strip()
            parts = [f"- {title}"]
            if kind:
                parts.append(f"[{kind}]")
            if local_rel:
                parts.append(f"(local: {local_rel})")
            if url and (not local_rel):
                parts.append(f"({url})")
            lines.append(" ".join(parts))
        lines.append("")

    # Simple learning-oriented nudges
    if materials or zoom_links:
        lines.append("## How to use this (learning-first)")
        lines.append("")
        lines.append("- Skim the instructions above, then open the materials in order.")
        if any((m.get("resource_category") == "reading") or (m.get("material_kind") == "reading") for m in materials):
            lines.append("- While reading, write down 3 key takeaways + 2 questions.")
        if any(_is_video_file_relpath(m.get("local_relative_path", "")) for m in materials) or zoom_links:
            lines.append("- While watching, pause to summarize each segment in 1–2 sentences.")
        lines.append("- End by making a short checklist of what you can now explain from memory.")
        lines.append("")

    out_path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
    return str(out_path.relative_to(download_dir))


def bundle_weekly_exports(download_dir: Path) -> None:
    """Create ALL-inclusive weekly bundles under download_dir/_weekly/."""
    weekly_dir = download_dir / "_weekly"
    weekly_dir.mkdir(parents=True, exist_ok=True)

    local_tz = datetime.now().astimezone().tzinfo or timezone.utc

    # We’ll build: tasks (assignments/quizzes/prep) + resources (module content + linked materials)
    all_items: list[dict] = []
    manifests = sorted(download_dir.glob("*/_manifest.json"))

    # Index of manifest items to help attach local files to resources.
    # Keys:
    # - (course_id, item_id) -> item
    # - (course_id, file_numeric_id) -> item
    items_by_id: dict[tuple[str, str], dict] = {}
    file_items_by_file_id: dict[tuple[str, str], dict] = {}
    items_by_module_folder: dict[tuple[str, str], list[dict]] = {}

    # First pass: load manifests and build indexes
    manifests_loaded: list[tuple[Path, dict]] = []
    for manifest_path in manifests:
        try:
            with open(manifest_path, "r") as f:
                manifest = json.load(f)
        except Exception:
            continue
        manifests_loaded.append((manifest_path, manifest))

        course = manifest.get("course") or {}
        course_id = str(course.get("id", "") or "")
        for it in (manifest.get("items") or []):
            iid = str(it.get("item_id") or "")
            if iid:
                items_by_id[(course_id, iid)] = it
            if (it.get("item_type") or "").strip().lower() == "file":
                m = re.match(r"^(?:file|linked_file)_(\d+)$", iid)
                if m:
                    fid = m.group(1)
                    file_items_by_file_id[(course_id, fid)] = it

            module_folder = _infer_module_folder_from_relative_path(it.get("file_path"))
            if module_folder:
                items_by_module_folder.setdefault((course_id, module_folder), []).append(it)

    # Second pass: build per-week buckets with dedupe
    by_week: dict[str, dict[str, dict]] = {}
    unscheduled: list[dict] = []

    def add_to_week(week_key: Optional[str], item: dict):
        if not week_key:
            unscheduled.append(item)
            return
        by_week.setdefault(week_key, {})
        by_week[week_key][item["id"]] = item

    def week_anchor_dt(week_key: str) -> datetime:
        start, _ = _week_start_end_dates(week_key)
        # anchor resources to Monday 09:00 local
        return datetime(start.year, start.month, start.day, 9, 0, 0, tzinfo=local_tz)

    for manifest_path, manifest in manifests_loaded:
        course = manifest.get("course") or {}
        course_id = str(course.get("id", "") or "")
        course_name = course.get("name") or manifest_path.parent.name
        course_url = course.get("url") or ""

        for item in (manifest.get("items") or []):
            item_type = (item.get("item_type") or "").strip().lower()
            title = item.get("title") or ""

            is_graded = item_type in {"assignment", "quiz"}
            if not is_graded:
                continue

            due_at = item.get("due_at")
            if not due_at:
                due_at = _infer_due_at_from_text_file(download_dir, item.get("file_path"))

            scheduled_dt = None
            if due_at:
                dt = _parse_canvas_datetime(due_at)
                if dt:
                    scheduled_dt = dt.astimezone(local_tz)

            week_key = _iso_week_key(scheduled_dt) if scheduled_dt else None
            direct_url = _stable_canvas_url_for_item(
                course_id=course_id,
                item_type=item_type,
                item_id=item.get("item_id"),
                source_url=item.get("source_url"),
            )

            module_folder = _infer_module_folder_from_relative_path(item.get("file_path"))

            base = {
                "id": item.get("item_id"),
                "kind": "assignment" if item_type == "assignment" else "quiz",
                "title": title,
                "course": {"id": course_id, "name": course_name, "url": course_url},
                "direct_url": direct_url,
                "source_url": item.get("source_url"),
                "local_relative_path": item.get("file_path"),
                "module": {
                    "id": item.get("module_id"),
                    "name": item.get("module_name"),
                    "folder": module_folder,
                    "unlock_at": item.get("module_unlock_at") or None,
                },
                "due_at": due_at,
                "scheduled_by": "due_at" if scheduled_dt else None,
                "links": item.get("links") or [],
                "scheduled_at_local": scheduled_dt.isoformat() if scheduled_dt else None,
                "scheduled_date_local": scheduled_dt.date().isoformat() if scheduled_dt else None,
                "week": week_key,
            }

            # Build per-task "materials" so the UI can show everything needed *inside* the task card.
            materials: list[dict] = []
            zoom_links: list[dict] = []

            # A) Links referenced directly from this assignment/quiz
            for link in (base.get("links") or []):
                url = (link.get("resolved_url") or link.get("url") or "").strip()
                if not url:
                    continue
                if base.get("direct_url") and url == base["direct_url"]:
                    continue

                if is_zoom_related(url, link.get("title", ""), title):
                    zoom_links.append(
                        {
                            "title": (link.get("text") or link.get("title") or "").strip(),
                            "url": url,
                            "source": "linked_from_task",
                        }
                    )

                kind = _resource_kind_from_url(url, link.get("type", ""))
                title_hint = (link.get("text") or link.get("title") or "").strip()

                local_path = None
                file_id = None
                m = re.search(r"/files/(\d+)", url)
                if m:
                    file_id = m.group(1)
                if file_id:
                    file_item = file_items_by_file_id.get((course_id, file_id))
                    if file_item:
                        local_path = file_item.get("file_path")

                mat = {
                    "material_kind": kind,
                    "title": title_hint or url,
                    "url": url,
                    "direct_url": (f"{CANVAS_URL}/courses/{course_id}/files/{file_id}" if file_id else url),
                    "local_relative_path": local_path,
                }
                # If the linked file is a local video, treat it as video/recording.
                if local_path and _is_video_file_relpath(local_path):
                    mat["material_kind"] = "video"
                    if _looks_like_recording(mat["title"], local_path) or "zoom" in (mat["title"] or "").lower():
                        mat["material_kind"] = "recording"
                if _looks_like_reading(mat["title"]):
                    mat["material_kind"] = "reading"
                    mat["resource_category"] = "reading"
                    mat["reading"] = _extract_reading_spec(mat["title"])
                materials.append(mat)

            # B) Everything inside the same module folder (pages/files/videos/ppts/etc.)
            if module_folder:
                for mit in items_by_module_folder.get((course_id, module_folder), []):
                    mit_type = (mit.get("item_type") or "").strip().lower()
                    if mit_type in {"assignment", "quiz", "module"}:
                        continue
                    if mit_type not in {"page", "file", "external_url", "external_tool", "discussion"}:
                        continue
                    rid = str(mit.get("item_id") or "").strip()
                    if not rid:
                        continue

                    direct2 = _stable_canvas_url_for_item(
                        course_id=course_id,
                        item_type=mit_type,
                        item_id=rid,
                        source_url=mit.get("source_url"),
                    )
                    local_rel = mit.get("file_path")
                    mtitle = mit.get("title") or rid
                    mat2 = {
                        "material_kind": "module_item",
                        "resource_type": mit_type,
                        "title": mtitle,
                        "direct_url": direct2,
                        "source_url": mit.get("source_url"),
                        "local_relative_path": local_rel,
                        "links": mit.get("links") or [],
                    }

                    if is_zoom_related(direct2 or "", mtitle, title):
                        zoom_links.append({"title": mtitle, "url": direct2, "source": "module_item"})

                    # Promote obvious recordings / videos
                    if mit_type == "file":
                        if local_rel and _is_video_file_relpath(local_rel):
                            mat2["material_kind"] = "video"
                        if _looks_like_recording(mtitle, local_rel or "") or ("zoom" in (mtitle or "").lower()):
                            mat2["material_kind"] = "recording"

                    if _looks_like_reading(mtitle):
                        mat2["material_kind"] = "reading"
                        mat2["resource_category"] = "reading"
                        mat2["reading"] = _extract_reading_spec(mtitle)

                    materials.append(mat2)

            base["materials"] = materials
            base["zoom"] = zoom_links
            base["priority_hint"] = _priority_hint(base["kind"], base.get("title") or "")

            all_items.append(base)
            add_to_week(week_key, base)

            # Prep item
            if scheduled_dt:
                offset_days = 3 if item_type == "assignment" else 2
                prep_dt = scheduled_dt - timedelta(days=offset_days)
                prep_week = _iso_week_key(prep_dt)
                actions = _summarize_prep_focus(materials, zoom_links)
                actions_str = ", ".join(actions)
                prep = {
                    "id": f"prep::{base['id']}::-{offset_days}d",
                    "kind": "prep",
                    "title": f"Prep: {actions_str} for {base['kind']} – {title}",
                    "course": {"id": course_id, "name": course_name, "url": course_url},
                    "direct_url": direct_url,
                    "source_url": base.get("source_url"),
                    "local_relative_path": base.get("local_relative_path"),
                    "module": base.get("module"),
                    "due_at": None,
                    "scheduled_by": "generated_prep",
                    "generated_from": {"id": base["id"], "kind": base["kind"], "offset_days": -offset_days},
                    "scheduled_at_local": prep_dt.isoformat(),
                    "scheduled_date_local": prep_dt.date().isoformat(),
                    "week": prep_week,
                }
                prep["materials"] = materials
                prep["zoom"] = zoom_links
                prep["priority_hint"] = _priority_hint(prep["kind"], prep.get("title") or "")
                all_items.append(prep)
                add_to_week(prep_week, prep)

            # Resource items:
            # 1) Links referenced directly from this assignment/quiz
            if week_key:
                anchor = week_anchor_dt(week_key)

                for link in (base.get("links") or []):
                    url = (link.get("resolved_url") or link.get("url") or "").strip()
                    if not url:
                        continue
                    # Skip “self” references (Canvas Assignment/Quiz Page links we add to the item itself)
                    if base.get("direct_url") and url == base["direct_url"]:
                        continue
                    if (link.get("type") or "").strip().lower() in {"canvas_assignment", "canvas_quiz"}:
                        continue

                    kind = _resource_kind_from_url(url, link.get("type", ""))
                    title_hint = (link.get("text") or link.get("title") or "").strip()

                    file_id = None
                    m = re.search(r"/files/(\d+)", url)
                    if m:
                        file_id = m.group(1)
                    resource_id = None
                    if file_id:
                        resource_id = f"resource:file:{course_id}:{file_id}"
                    else:
                        resource_id = f"resource:url:{hashlib.sha1(url.encode('utf-8')).hexdigest()[:12]}"

                    local_path = None
                    direct = url
                    if file_id:
                        direct = f"{CANVAS_URL}/courses/{course_id}/files/{file_id}"
                        file_item = file_items_by_file_id.get((course_id, file_id))
                        if file_item:
                            local_path = file_item.get("file_path")

                    res = {
                        "id": resource_id,
                        "kind": "resource",
                        "resource_type": kind,
                        "title": title_hint or url,
                        "course": {"id": course_id, "name": course_name, "url": course_url},
                        "direct_url": direct,
                        "url": url,
                        "local_relative_path": local_path,
                        "scheduled_by": f"linked_from:{base['id']}",
                        "scheduled_at_local": anchor.isoformat(),
                        "scheduled_date_local": anchor.date().isoformat(),
                        "week": week_key,
                    }
                    if _looks_like_reading(res["title"]):
                        res["resource_category"] = "reading"
                        res["reading"] = _extract_reading_spec(res["title"])
                    all_items.append(res)
                    add_to_week(week_key, res)

                # 2) Everything inside the same module folder (pages/files/videos/ppts/etc.)
                if module_folder:
                    for mit in items_by_module_folder.get((course_id, module_folder), []):
                        mit_type = (mit.get("item_type") or "").strip().lower()
                        if mit_type in {"assignment", "quiz", "module"}:
                            continue
                        if mit_type not in {"page", "file", "external_url", "external_tool", "discussion"}:
                            continue
                        rid = str(mit.get("item_id") or "").strip()
                        if not rid:
                            continue
                        res_id = f"resource:item:{course_id}:{rid}"
                        direct2 = _stable_canvas_url_for_item(
                            course_id=course_id,
                            item_type=mit_type,
                            item_id=rid,
                            source_url=mit.get("source_url"),
                        )
                        r = {
                            "id": res_id,
                            "kind": "resource",
                            "resource_type": mit_type,
                            "title": mit.get("title") or rid,
                            "course": {"id": course_id, "name": course_name, "url": course_url},
                            "direct_url": direct2,
                            "source_url": mit.get("source_url"),
                            "local_relative_path": mit.get("file_path"),
                            "module": {
                                "id": mit.get("module_id"),
                                "name": mit.get("module_name"),
                                "folder": module_folder,
                                "unlock_at": mit.get("module_unlock_at") or None,
                            },
                            "links": mit.get("links") or [],
                            "scheduled_by": f"module_of:{base['id']}",
                            "scheduled_at_local": anchor.isoformat(),
                            "scheduled_date_local": anchor.date().isoformat(),
                            "week": week_key,
                        }
                        if _looks_like_reading(r["title"]):
                            r["resource_category"] = "reading"
                            r["reading"] = _extract_reading_spec(r["title"])
                        all_items.append(r)
                        add_to_week(week_key, r)

    # Write per-week folders (skip future weeks - only create folders for weeks that have started)
    today = date.today()
    index = {"generated_at": datetime.now().isoformat(), "weeks": []}
    skipped_future = 0
    for week_key in sorted(by_week.keys()):
        start, end = _week_start_end_dates(week_key)
        # Skip future weeks - only create folders for weeks that have started
        if start > today:
            skipped_future += 1
            continue
        week_folder = weekly_dir / f"{week_key}_{start.isoformat()}_to_{end.isoformat()}"
        week_folder.mkdir(parents=True, exist_ok=True)

        payload = {
            "week": {"key": week_key, "start_date": start.isoformat(), "end_date": end.isoformat()},
            "generated_at": datetime.now().isoformat(),
            "items": sorted(
                list(by_week[week_key].values()),
                key=lambda x: (
                    x.get("scheduled_at_local") or "",
                    x.get("course", {}).get("name", ""),
                    x.get("kind", ""),
                    x.get("resource_type", ""),
                    x.get("title", ""),
                ),
            ),
        }

        # Create per-task bundle files (so the UI can open a self-contained task instead of deep links).
        for it in payload["items"]:
            if it.get("kind") not in {"assignment", "quiz", "prep"}:
                continue
            rel = _write_task_bundle_file(
                download_dir=download_dir,
                week_folder=week_folder,
                task=it,
                materials=it.get("materials") or [],
                zoom_links=it.get("zoom") or [],
            )
            if rel:
                it["task_bundle_relative_path"] = rel

        with open(week_folder / "week.json", "w") as f:
            json.dump(payload, f, indent=2)

        index["weeks"].append({"key": week_key, "folder": week_folder.name, "count": len(payload["items"])})

    if skipped_future > 0:
        print(f"   ⏭️  Skipped {skipped_future} future week(s) (details not available yet)")

    with open(weekly_dir / "_index.json", "w") as f:
        json.dump(index, f, indent=2)

    with open(weekly_dir / "_all_items.json", "w") as f:
        json.dump({"generated_at": datetime.now().isoformat(), "items": all_items}, f, indent=2)

    with open(weekly_dir / "_unscheduled.json", "w") as f:
        json.dump({"generated_at": datetime.now().isoformat(), "items": unscheduled}, f, indent=2)


async def main():
    import sys
    
    force_sync = "--force" in sys.argv or "-f" in sys.argv
    bundle_weeks = "--bundle-weeks" in sys.argv
    bundle_only = "--bundle-only" in sys.argv
    
    # Check for course filter
    course_filter = None
    for i, arg in enumerate(sys.argv):
        if arg in ["--course", "-c"] and i + 1 < len(sys.argv):
            course_filter = sys.argv[i + 1]

    if bundle_only:
        print(f"📂 Building weekly bundles from: {DOWNLOAD_DIR}")
        bundle_weekly_exports(DOWNLOAD_DIR)
        print(f"✅ Weekly bundles saved to: {DOWNLOAD_DIR / '_weekly'}")
        return
    
    syncer = CanvasSync(force_sync=force_sync, course_filter=course_filter, bundle_weeks=bundle_weeks)
    await syncer.run()


if __name__ == "__main__":
    asyncio.run(main())
