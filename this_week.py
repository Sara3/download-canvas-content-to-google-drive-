"""
This Week: Focus on what's due NOW
Shows only current week's assignments and any overdue items

Usage:
    python this_week.py           # Show this week's work
    python this_week.py --prep    # Generate study materials for this week only
"""

import os
import re
import json
from pathlib import Path
from datetime import datetime, timedelta
from collections import defaultdict
from dotenv import load_dotenv

# Load .env from script directory
SCRIPT_DIR = Path(__file__).parent
load_dotenv(SCRIPT_DIR / ".env")

CANVAS_DIR = Path(os.getenv("DOWNLOAD_DIR", "./canvas_downloads"))


def parse_due_date(date_str: str) -> datetime | None:
    """Parse various date formats from Canvas."""
    if not date_str or date_str == "No due date":
        return None
    
    # ISO format: 2026-01-26T07:59:00Z
    try:
        if "T" in date_str:
            return datetime.fromisoformat(date_str.replace("Z", "+00:00")).replace(tzinfo=None)
    except:
        pass
    
    # Try other formats
    formats = [
        "%Y-%m-%d",
        "%B %d, %Y",
        "%b %d, %Y",
    ]
    for fmt in formats:
        try:
            return datetime.strptime(date_str, fmt)
        except:
            continue
    
    return None


def get_week_bounds(reference_date: datetime = None) -> tuple[datetime, datetime]:
    """Get start (Monday) and end (Sunday) of the current week."""
    if reference_date is None:
        reference_date = datetime.now()
    
    # Monday of current week
    start = reference_date - timedelta(days=reference_date.weekday())
    start = start.replace(hour=0, minute=0, second=0, microsecond=0)
    
    # Sunday end of week
    end = start + timedelta(days=6, hours=23, minutes=59, seconds=59)
    
    return start, end


def scan_assignments() -> list[dict]:
    """Scan all downloaded assignments for due dates."""
    assignments = []
    
    # Pattern to extract due date from assignment files
    due_pattern = r'Due:\s*(.+?)(?:\n|$)'
    points_pattern = r'Points:\s*(.+?)(?:\n|$)'
    
    for txt_file in CANVAS_DIR.rglob("*.txt"):
        try:
            content = txt_file.read_text(encoding='utf-8')
            
            # Check if it's an assignment file
            if "ASSIGNMENT:" not in content and "QUIZ:" not in content:
                continue
            
            # Extract title
            title_match = re.search(r'(?:ASSIGNMENT|QUIZ):\s*(.+?)(?:\n|=)', content)
            title = title_match.group(1).strip() if title_match else txt_file.stem
            
            # Extract due date
            due_match = re.search(due_pattern, content)
            due_str = due_match.group(1).strip() if due_match else None
            due_date = parse_due_date(due_str) if due_str else None
            
            # Extract points
            points_match = re.search(points_pattern, content)
            points = points_match.group(1).strip() if points_match else "N/A"
            
            # Determine course from path
            rel_path = txt_file.relative_to(CANVAS_DIR)
            course = str(rel_path.parts[0]) if rel_path.parts else "Unknown"
            
            # Determine type
            content_type = "quiz" if "QUIZ:" in content else "assignment"
            
            assignments.append({
                "title": title,
                "course": course,
                "due_date": due_date,
                "due_str": due_str,
                "points": points,
                "type": content_type,
                "file_path": str(txt_file),
                "content": content[:1000],  # First 1000 chars for context
            })
            
        except Exception as e:
            continue
    
    return assignments


def categorize_assignments(assignments: list[dict]) -> dict:
    """Categorize assignments into overdue, this week, and future."""
    now = datetime.now()
    week_start, week_end = get_week_bounds(now)
    
    # Deduplicate by title + course + due_date
    seen = set()
    unique_assignments = []
    for a in assignments:
        key = (a["title"].lower(), a["course"], a["due_date"])
        if key not in seen:
            seen.add(key)
            unique_assignments.append(a)
    
    categories = {
        "overdue": [],
        "this_week": [],
        "future": [],
        "no_date": [],
    }
    
    for a in unique_assignments:
        if a["due_date"] is None:
            categories["no_date"].append(a)
        elif a["due_date"] < now:
            categories["overdue"].append(a)
        elif week_start <= a["due_date"] <= week_end:
            categories["this_week"].append(a)
        else:
            categories["future"].append(a)
    
    # Sort by due date
    for key in ["overdue", "this_week", "future"]:
        categories[key].sort(key=lambda x: x["due_date"])
    
    return categories


def format_assignment(a: dict, show_course: bool = True) -> str:
    """Format a single assignment for display."""
    due_str = a["due_date"].strftime("%a %b %d, %I:%M %p") if a["due_date"] else "No due date"
    course_short = a["course"][:40] + "..." if len(a["course"]) > 40 else a["course"]
    
    icon = "❓" if a["type"] == "quiz" else "📝"
    
    lines = [f"{icon} {a['title']}"]
    if show_course:
        lines.append(f"   📚 {course_short}")
    lines.append(f"   📅 Due: {due_str}")
    if a["points"] != "N/A":
        lines.append(f"   ⭐ {a['points']} points")
    
    return "\n".join(lines)


def display_this_week():
    """Display this week's assignments."""
    print("""
╔═══════════════════════════════════════════════════════════╗
║               📅 THIS WEEK'S ASSIGNMENTS                   ║
╚═══════════════════════════════════════════════════════════╝
    """)
    
    now = datetime.now()
    week_start, week_end = get_week_bounds(now)
    print(f"Week of {week_start.strftime('%B %d')} - {week_end.strftime('%B %d, %Y')}")
    print(f"Today: {now.strftime('%A, %B %d')}")
    print()
    
    print("🔍 Scanning assignments...")
    assignments = scan_assignments()
    print(f"   Found {len(assignments)} total assignments/quizzes")
    
    categories = categorize_assignments(assignments)
    
    # Show overdue first (RED ALERT!)
    if categories["overdue"]:
        print("\n" + "=" * 50)
        print("🚨 OVERDUE - NEEDS IMMEDIATE ATTENTION")
        print("=" * 50)
        for a in categories["overdue"]:
            days_overdue = (now - a["due_date"]).days
            print(f"\n{format_assignment(a)}")
            print(f"   ⚠️  {days_overdue} days overdue!")
    
    # This week's work
    if categories["this_week"]:
        print("\n" + "=" * 50)
        print("📌 DUE THIS WEEK")
        print("=" * 50)
        
        # Group by day
        by_day = defaultdict(list)
        for a in categories["this_week"]:
            day_key = a["due_date"].strftime("%A, %b %d")
            by_day[day_key].append(a)
        
        for day, items in by_day.items():
            print(f"\n📆 {day}")
            print("-" * 30)
            for a in items:
                print(f"\n{format_assignment(a, show_course=True)}")
    else:
        print("\n✅ No assignments due this week!")
    
    # Summary
    print("\n" + "=" * 50)
    print("📊 SUMMARY")
    print("=" * 50)
    print(f"   🚨 Overdue: {len(categories['overdue'])}")
    print(f"   📌 This Week: {len(categories['this_week'])}")
    print(f"   📅 Future: {len(categories['future'])}")
    print(f"   ❓ No Date: {len(categories['no_date'])}")
    
    # Prep recommendations
    if categories["this_week"] or categories["overdue"]:
        print("\n" + "=" * 50)
        print("📚 STUDY PREP RECOMMENDATIONS")
        print("=" * 50)
        
        all_urgent = categories["overdue"] + categories["this_week"]
        courses_with_work = set(a["course"] for a in all_urgent)
        
        for course in courses_with_work:
            course_items = [a for a in all_urgent if a["course"] == course]
            print(f"\n📖 {course[:50]}...")
            for a in course_items[:3]:  # Top 3 per course
                print(f"   • {a['title'][:40]}...")
    
    return categories


def generate_weekly_prep(categories: dict):
    """Generate study materials for this week's assignments."""
    import asyncio
    from study_prep import get_chapter_summary, save_summary
    
    all_urgent = categories["overdue"] + categories["this_week"]
    
    if not all_urgent:
        print("No urgent assignments to prepare for!")
        return
    
    print("\n📚 Generating study materials for this week...")
    
    # Extract reading references from assignment content
    chapter_pattern = r'chapter[s]?\s+(\d+(?:\s*[-–&,]\s*\d+)*)'
    
    for a in all_urgent:
        content = a.get("content", "")
        matches = re.findall(chapter_pattern, content.lower())
        
        if matches:
            print(f"\n📖 Found chapter references in: {a['title'][:40]}...")
            for chapters in matches:
                print(f"   Chapters: {chapters}")
                # Would call get_chapter_summary here
    
    print("\n✅ Run 'python study_prep.py --scan' to generate all summaries")


def main():
    import sys
    
    categories = display_this_week()
    
    if len(sys.argv) > 1 and sys.argv[1] == "--prep":
        generate_weekly_prep(categories)


if __name__ == "__main__":
    main()
