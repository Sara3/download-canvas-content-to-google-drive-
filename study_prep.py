"""
Study Prep: Extract chapter summaries from textbooks you don't own
Uses LLM knowledge + web search to get key concepts

Usage:
    python study_prep.py "Health & Wellness Coaching chapters 1-2"
    python study_prep.py --scan  # Scan all reading assignments
"""

import os
import re
import json
import asyncio
from pathlib import Path
from datetime import datetime
import httpx
from dotenv import load_dotenv

# Load .env from script directory
SCRIPT_DIR = Path(__file__).parent
load_dotenv(SCRIPT_DIR / ".env")

# Configuration
CANVAS_DIR = Path(os.getenv("DOWNLOAD_DIR", "./canvas_downloads"))
STUDY_DIR = CANVAS_DIR / "_study_summaries"
PERPLEXITY_API_KEY = os.getenv("PERPLEXITY_API_KEY", "")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")


def find_reading_assignments() -> list[dict]:
    """Scan downloaded content for reading assignments mentioning chapters."""
    readings = []
    
    # Patterns to find chapter references
    patterns = [
        r'read\s+chapter[s]?\s+(\d+(?:\s*[-–&,]\s*\d+)*)',
        r'chapter[s]?\s+(\d+(?:\s*[-–&,]\s*\d+)*)\s+(?:of|from|in)',
        r'textbook\s+chapter[s]?\s+(\d+(?:\s*[-–&,]\s*\d+)*)',
        r'reading[s]?:\s*chapter[s]?\s+(\d+(?:\s*[-–&,]\s*\d+)*)',
    ]
    
    for txt_file in CANVAS_DIR.rglob("*.txt"):
        try:
            content = txt_file.read_text(encoding='utf-8')
            content_lower = content.lower()
            
            for pattern in patterns:
                matches = re.finditer(pattern, content_lower)
                for match in matches:
                    # Get context around the match
                    start = max(0, match.start() - 200)
                    end = min(len(content), match.end() + 200)
                    context = content[start:end]
                    
                    readings.append({
                        "file": str(txt_file.relative_to(CANVAS_DIR)),
                        "chapters": match.group(1),
                        "context": context.strip(),
                        "course": txt_file.parts[len(CANVAS_DIR.parts)] if len(txt_file.parts) > len(CANVAS_DIR.parts) else "Unknown"
                    })
        except Exception as e:
            continue
    
    return readings


def extract_book_info(context: str) -> dict:
    """Try to extract book title from context."""
    # Common textbook patterns
    book_patterns = [
        r'"([^"]+)"',  # Quoted title
        r'textbook[:\s]+([A-Z][^\.]+)',  # After "textbook:"
        r'from\s+([A-Z][^\.]+?)(?:\s+chapter|\s+ch\.)',  # "from [Title] chapter"
    ]
    
    for pattern in book_patterns:
        match = re.search(pattern, context, re.IGNORECASE)
        if match:
            return {"title": match.group(1).strip()}
    
    return {"title": "course textbook"}


async def get_chapter_summary_perplexity(book_title: str, chapters: str, course_context: str = "") -> str:
    """Use Perplexity API to get chapter summaries with web search."""
    if not PERPLEXITY_API_KEY:
        return None
    
    prompt = f"""I'm studying for a college course and need a comprehensive summary of the key concepts.

Book: {book_title}
Chapters: {chapters}
{f"Course context: {course_context}" if course_context else ""}

Please provide:
1. Main topics and key concepts covered in these chapters
2. Important definitions and terms
3. Key takeaways a student should know for exams
4. Any formulas, frameworks, or models introduced

Format this as clear study notes that I can review or listen to as audio."""

    try:
        async with httpx.AsyncClient(timeout=60) as client:
            resp = await client.post(
                "https://api.perplexity.ai/chat/completions",
                headers={
                    "Authorization": f"Bearer {PERPLEXITY_API_KEY}",
                    "Content-Type": "application/json"
                },
                json={
                    "model": "sonar",
                    "messages": [{"role": "user", "content": prompt}],
                    "temperature": 0.3
                }
            )
            
            if resp.status_code == 200:
                data = resp.json()
                return data["choices"][0]["message"]["content"]
    except Exception as e:
        print(f"Perplexity error: {e}")
    
    return None


async def get_chapter_summary_openai(book_title: str, chapters: str, course_context: str = "") -> str:
    """Use OpenAI API to get chapter summaries from LLM knowledge."""
    if not OPENAI_API_KEY:
        return None
    
    prompt = f"""I'm studying for a college course and need a comprehensive summary of the key concepts.

Book: {book_title}
Chapters: {chapters}
{f"Course context: {course_context}" if course_context else ""}

Based on your knowledge of this textbook (or similar textbooks on this topic), please provide:
1. Main topics and key concepts typically covered in these chapters
2. Important definitions and terms
3. Key takeaways a student should know
4. Any formulas, frameworks, or models typically introduced

Format this as clear study notes that I can review or listen to as audio.

Note: If you're not familiar with this specific textbook, provide a comprehensive overview of what these topics typically cover in college-level courses."""

    try:
        async with httpx.AsyncClient(timeout=60) as client:
            resp = await client.post(
                "https://api.openai.com/v1/chat/completions",
                headers={
                    "Authorization": f"Bearer {OPENAI_API_KEY}",
                    "Content-Type": "application/json"
                },
                json={
                    "model": "gpt-4o",
                    "messages": [{"role": "user", "content": prompt}],
                    "temperature": 0.3
                }
            )
            
            if resp.status_code == 200:
                data = resp.json()
                return data["choices"][0]["message"]["content"]
    except Exception as e:
        print(f"OpenAI error: {e}")
    
    return None


async def get_chapter_summary(book_title: str, chapters: str, course_context: str = "") -> str:
    """Get chapter summary using available APIs."""
    
    # Try Perplexity first (has web search)
    summary = await get_chapter_summary_perplexity(book_title, chapters, course_context)
    if summary:
        return summary
    
    # Fall back to OpenAI
    summary = await get_chapter_summary_openai(book_title, chapters, course_context)
    if summary:
        return summary
    
    return "No API keys configured. Add PERPLEXITY_API_KEY or OPENAI_API_KEY to .env"


def save_summary(book_title: str, chapters: str, summary: str, course: str = ""):
    """Save summary to study folder."""
    STUDY_DIR.mkdir(parents=True, exist_ok=True)
    
    # Create filename
    safe_title = re.sub(r'[^\w\s-]', '', book_title)[:50]
    safe_chapters = chapters.replace(' ', '').replace(',', '-')
    filename = f"{safe_title}_Ch{safe_chapters}.txt"
    
    if course:
        course_dir = STUDY_DIR / re.sub(r'[^\w\s-]', '', course)[:50]
        course_dir.mkdir(exist_ok=True)
        filepath = course_dir / filename
    else:
        filepath = STUDY_DIR / filename
    
    output = []
    output.append("=" * 60)
    output.append(f"STUDY NOTES: {book_title.upper()}")
    output.append(f"Chapters: {chapters}")
    output.append(f"Generated: {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    output.append("=" * 60)
    output.append("")
    output.append(summary)
    
    filepath.write_text("\n".join(output), encoding='utf-8')
    print(f"💾 Saved: {filepath}")
    return filepath


async def process_single_request(request: str):
    """Process a single study request like 'Health Coaching chapters 1-2'."""
    # Parse the request
    chapter_match = re.search(r'chapter[s]?\s+(\d+(?:\s*[-–&,]\s*\d+)*)', request, re.IGNORECASE)
    if not chapter_match:
        print("❌ Could not find chapter numbers in request")
        return
    
    chapters = chapter_match.group(1)
    book_title = request[:chapter_match.start()].strip()
    if not book_title:
        book_title = "course textbook"
    
    print(f"📚 Looking up: {book_title}, Chapters {chapters}")
    print("🔍 Searching for summaries...")
    
    summary = await get_chapter_summary(book_title, chapters)
    
    if summary:
        print("\n" + "=" * 60)
        print(summary[:500] + "..." if len(summary) > 500 else summary)
        print("=" * 60)
        
        save_summary(book_title, chapters, summary)


async def scan_and_summarize():
    """Scan all reading assignments and generate summaries."""
    print("🔍 Scanning for reading assignments...")
    
    readings = find_reading_assignments()
    
    if not readings:
        print("No reading assignments found mentioning chapters")
        return
    
    print(f"📚 Found {len(readings)} reading references")
    
    # Deduplicate
    seen = set()
    unique_readings = []
    for r in readings:
        key = (r['course'], r['chapters'])
        if key not in seen:
            seen.add(key)
            unique_readings.append(r)
    
    print(f"📖 {len(unique_readings)} unique reading assignments")
    
    for i, reading in enumerate(unique_readings, 1):
        book_info = extract_book_info(reading['context'])
        print(f"\n[{i}/{len(unique_readings)}] {reading['course']}")
        print(f"   📖 {book_info['title']}, Chapters {reading['chapters']}")
        
        # Check if already summarized
        safe_title = re.sub(r'[^\w\s-]', '', book_info['title'])[:50]
        safe_chapters = reading['chapters'].replace(' ', '').replace(',', '-')
        existing = STUDY_DIR / f"{safe_title}_Ch{safe_chapters}.txt"
        
        if existing.exists():
            print(f"   ⏭️ Already summarized")
            continue
        
        summary = await get_chapter_summary(
            book_info['title'], 
            reading['chapters'],
            reading['course']
        )
        
        if summary and not summary.startswith("No API"):
            save_summary(book_info['title'], reading['chapters'], summary, reading['course'])
        else:
            print(f"   ⚠️ {summary}")


async def main():
    import sys
    
    print("""
╔═══════════════════════════════════════════════════════════╗
║           Study Prep: Get Chapter Summaries               ║
╚═══════════════════════════════════════════════════════════╝
    """)
    
    if len(sys.argv) > 1:
        if sys.argv[1] == "--scan":
            await scan_and_summarize()
        else:
            # Process single request
            request = " ".join(sys.argv[1:])
            await process_single_request(request)
    else:
        print("Usage:")
        print('  python study_prep.py "Book Title chapters 1-2"')
        print('  python study_prep.py --scan  # Scan all reading assignments')
        print("")
        print("Configure API keys in .env:")
        print("  PERPLEXITY_API_KEY=...  (recommended - has web search)")
        print("  OPENAI_API_KEY=...      (fallback)")


if __name__ == "__main__":
    asyncio.run(main())
