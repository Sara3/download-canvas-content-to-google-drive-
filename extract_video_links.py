"""
Extract Video Links
===================

Scans all downloaded Canvas content and extracts:
- Zoom recordings
- YouTube videos
- Other video/lecture links

Outputs a single organized file with all playable links.
"""

import os
import re
import json
from pathlib import Path
from collections import defaultdict
from datetime import datetime
from dotenv import load_dotenv

# Load environment
SCRIPT_DIR = Path(__file__).parent
load_dotenv(SCRIPT_DIR / ".env")

CANVAS_DIR = Path(os.getenv("DOWNLOAD_DIR", "./canvas_downloads"))

# Regex patterns for video links
VIDEO_PATTERNS = [
    # Zoom recordings
    (r'https://[^/]*zoom\.us/rec/[^\s\"\'\)]+', 'Zoom Recording'),
    (r'https://[^/]*zoom\.us/j/[^\s\"\'\)]+', 'Zoom Meeting'),
    # YouTube
    (r'https://(?:www\.)?youtube\.com/watch\?v=[^\s\"\'\)&]+', 'YouTube'),
    (r'https://youtu\.be/[^\s\"\'\)]+', 'YouTube'),
    # Instructure Media
    (r'https://[^/]*instructuremedia\.com/[^\s\"\'\)]+', 'Instructure Media'),
    # Vimeo
    (r'https://(?:www\.)?vimeo\.com/[^\s\"\'\)]+', 'Vimeo'),
    # Kaltura (common in Canvas)
    (r'https://[^/]*kaltura[^\s\"\'\)]+', 'Kaltura'),
    # Generic video URLs
    (r'https://[^\s\"\'\)]*(?:video|lecture|recording)[^\s\"\'\)]*', 'Video Link'),
]


def extract_links_from_file(filepath: Path) -> list:
    """Extract video links from a file."""
    links = []
    
    try:
        content = filepath.read_text(encoding='utf-8', errors='ignore')
        
        for pattern, link_type in VIDEO_PATTERNS:
            matches = re.findall(pattern, content, re.IGNORECASE)
            for match in matches:
                # Clean up the URL
                url = match.rstrip('.,;:)')
                links.append({
                    'url': url,
                    'type': link_type,
                    'source_file': str(filepath),
                })
    except Exception as e:
        pass
    
    return links


def extract_links_from_url_file(filepath: Path) -> list:
    """Extract URL from .url shortcut files."""
    links = []
    
    try:
        content = filepath.read_text(encoding='utf-8', errors='ignore')
        url_match = re.search(r'URL=(.+)', content)
        
        if url_match:
            url = url_match.group(1).strip()
            
            # Check if it's a video link
            for pattern, link_type in VIDEO_PATTERNS:
                if re.search(pattern, url, re.IGNORECASE):
                    links.append({
                        'url': url,
                        'type': link_type,
                        'source_file': str(filepath),
                        'title': filepath.stem,
                    })
                    break
    except Exception:
        pass
    
    return links


def get_course_name(filepath: Path) -> str:
    """Extract course name from file path."""
    parts = filepath.parts
    
    for part in parts:
        if 'FDNT' in part or 'KIN' in part:
            # Clean up course name
            # Spring 2026 enrolled courses (updated 01/21/2026)
            if 'FDNT10' in part:
                return 'FDNT10: Elementary Nutrition'
            elif 'KIN53' in part:
                return 'KIN53: Principles of Health and Wellness'
            elif 'KIN70' in part:
                return 'KIN70: Yoga Techniques'
            elif 'KIN73' in part:
                return 'KIN73: Fitness Walking'
            elif 'KIN84' in part:
                return 'KIN84: Health and Wellness Coaching'
            return part[:50]
    
    return 'Unknown Course'


def get_context(filepath: Path) -> str:
    """Get context (module/week) from file path."""
    parts = filepath.parts
    
    for part in parts:
        if 'Week' in part or 'Module' in part or 'Weekend' in part:
            return part
    
    return filepath.parent.name


def main():
    print("""
╔═══════════════════════════════════════════════════════════╗
║           🎬 EXTRACTING VIDEO & LECTURE LINKS             ║
╚═══════════════════════════════════════════════════════════╝
    """)
    
    all_links = []
    
    # Scan all text files
    print("📂 Scanning downloaded content...")
    
    for filepath in CANVAS_DIR.rglob("*.txt"):
        if '_podcasts' in str(filepath) or '_study_summaries' in str(filepath):
            continue
        links = extract_links_from_file(filepath)
        all_links.extend(links)
    
    # Scan all .url shortcut files
    for filepath in CANVAS_DIR.rglob("*.url"):
        links = extract_links_from_url_file(filepath)
        all_links.extend(links)
    
    # Scan manifest files
    for filepath in CANVAS_DIR.rglob("_manifest.json"):
        try:
            with open(filepath) as f:
                manifest = json.load(f)
            
            for item in manifest:
                url = item.get('url', '')
                if url:
                    for pattern, link_type in VIDEO_PATTERNS:
                        if re.search(pattern, url, re.IGNORECASE):
                            all_links.append({
                                'url': url,
                                'type': link_type,
                                'source_file': str(filepath),
                                'title': item.get('title', ''),
                            })
                            break
        except Exception:
            pass
    
    # Deduplicate by URL
    seen_urls = set()
    unique_links = []
    for link in all_links:
        if link['url'] not in seen_urls:
            seen_urls.add(link['url'])
            unique_links.append(link)
    
    print(f"   Found {len(unique_links)} unique video/lecture links")
    
    # Organize by course
    by_course = defaultdict(list)
    for link in unique_links:
        course = get_course_name(Path(link['source_file']))
        context = get_context(Path(link['source_file']))
        link['context'] = context
        by_course[course].append(link)
    
    # Generate output
    output = []
    output.append("=" * 70)
    output.append("📺 LECTURE & VIDEO LINKS - Spring 2026")
    output.append("=" * 70)
    output.append(f"Generated: {datetime.now().strftime('%B %d, %Y at %I:%M %p')}")
    output.append(f"Total Links: {len(unique_links)}")
    output.append("")
    output.append("TIP: Click links on your phone to watch during commute!")
    output.append("=" * 70)
    output.append("")
    
    for course in sorted(by_course.keys()):
        links = by_course[course]
        
        output.append("")
        output.append("─" * 70)
        output.append(f"📚 {course}")
        output.append("─" * 70)
        output.append("")
        
        # Group by type
        by_type = defaultdict(list)
        for link in links:
            by_type[link['type']].append(link)
        
        for link_type in ['Zoom Recording', 'Zoom Meeting', 'YouTube', 'Instructure Media', 'Vimeo', 'Video Link']:
            type_links = by_type.get(link_type, [])
            if type_links:
                output.append(f"  🎬 {link_type}s ({len(type_links)}):")
                output.append("")
                
                for link in type_links:
                    title = link.get('title', link.get('context', 'Untitled'))
                    output.append(f"    • {title}")
                    output.append(f"      {link['url']}")
                    output.append("")
    
    # Summary section with all Zoom recordings
    zoom_recordings = [l for l in unique_links if l['type'] == 'Zoom Recording']
    if zoom_recordings:
        output.append("")
        output.append("=" * 70)
        output.append("🔴 ALL ZOOM RECORDINGS (for offline download)")
        output.append("=" * 70)
        output.append("")
        
        for link in zoom_recordings:
            course = get_course_name(Path(link['source_file']))
            context = link.get('context', '')
            output.append(f"• {course} - {context}")
            output.append(f"  {link['url']}")
            output.append("")
    
    # YouTube playlist
    youtube_links = [l for l in unique_links if l['type'] == 'YouTube']
    if youtube_links:
        output.append("")
        output.append("=" * 70)
        output.append("▶️ ALL YOUTUBE VIDEOS")
        output.append("=" * 70)
        output.append("")
        
        for link in youtube_links:
            title = link.get('title', 'Course Video')
            output.append(f"• {title}")
            output.append(f"  {link['url']}")
            output.append("")
    
    # Save output
    output_path = CANVAS_DIR / "_lecture_links.txt"
    with open(output_path, 'w', encoding='utf-8') as f:
        f.write('\n'.join(output))
    
    print(f"\n✅ Saved to: {output_path}")
    
    # Also save as JSON for programmatic access
    json_path = CANVAS_DIR / "_lecture_links.json"
    with open(json_path, 'w', encoding='utf-8') as f:
        json.dump({
            'generated_at': datetime.now().isoformat(),
            'total_links': len(unique_links),
            'by_course': {k: v for k, v in by_course.items()},
        }, f, indent=2)
    
    print(f"   JSON saved to: {json_path}")
    
    # Print summary
    print(f"""
📊 Summary:
   Zoom Recordings: {len([l for l in unique_links if l['type'] == 'Zoom Recording'])}
   Zoom Meetings:   {len([l for l in unique_links if l['type'] == 'Zoom Meeting'])}
   YouTube Videos:  {len([l for l in unique_links if l['type'] == 'YouTube'])}
   Other Videos:    {len([l for l in unique_links if l['type'] not in ['Zoom Recording', 'Zoom Meeting', 'YouTube']])}

📱 Your links are in Google Drive:
   Canvas/_lecture_links.txt
    """)


if __name__ == "__main__":
    main()
