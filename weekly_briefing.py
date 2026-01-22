"""
Weekly Study Briefing Generator
Creates a comprehensive study guide for the week with:
- What's due per course
- Required readings
- Announcements
- Study materials
- Everything you need to prepare

Usage:
    python weekly_briefing.py           # Generate this week's briefing
    python weekly_briefing.py --audio   # Also generate audio version
"""

import os
import re
from pathlib import Path
from datetime import datetime, timedelta
from collections import defaultdict
from dotenv import load_dotenv

# Load .env from script directory
SCRIPT_DIR = Path(__file__).parent
load_dotenv(SCRIPT_DIR / ".env")

CANVAS_DIR = Path(os.getenv("DOWNLOAD_DIR", "./canvas_downloads"))
BRIEFING_DIR = CANVAS_DIR / "_weekly_briefings"


def get_week_bounds(reference_date: datetime = None) -> tuple[datetime, datetime]:
    """Get start (Monday) and end (Sunday) of the current week."""
    if reference_date is None:
        reference_date = datetime.now()
    start = reference_date - timedelta(days=reference_date.weekday())
    start = start.replace(hour=0, minute=0, second=0, microsecond=0)
    end = start + timedelta(days=6, hours=23, minutes=59, seconds=59)
    return start, end


def parse_due_date(date_str: str) -> datetime | None:
    """Parse various date formats from Canvas."""
    if not date_str or date_str == "No due date":
        return None
    try:
        if "T" in date_str:
            return datetime.fromisoformat(date_str.replace("Z", "+00:00")).replace(tzinfo=None)
    except:
        pass
    return None


def get_course_name_short(course_path: str) -> str:
    """Get a friendly short name for the course."""
    # Spring 2026 enrolled courses (updated 01/21/2026)
    if "FDNT10" in course_path:
        return "Elementary Nutrition (FDNT10)"
    elif "KIN53" in course_path:
        return "Principles of Health and Wellness (KIN53)"
    elif "KIN70" in course_path:
        return "Yoga Techniques (KIN70)"
    elif "KIN73" in course_path:
        return "Fitness Walking (KIN73)"
    elif "KIN84" in course_path:
        return "Health and Wellness Coaching (KIN84)"
    return course_path[:50]


def scan_course_content(course_dir: Path) -> dict:
    """Scan a course folder for all relevant content."""
    content = {
        "assignments": [],
        "quizzes": [],
        "announcements": [],
        "readings": [],
        "pages": [],
        "syllabus": None,
    }
    
    # Patterns
    due_pattern = r'Due:\s*(.+?)(?:\n|$)'
    points_pattern = r'Points:\s*(.+?)(?:\n|$)'
    chapter_pattern = r'(?:read|chapter[s]?)\s+(?:chapter[s]?\s+)?(\d+(?:\s*[-–&,]\s*\d+)*)'
    
    for txt_file in course_dir.rglob("*.txt"):
        try:
            text = txt_file.read_text(encoding='utf-8')
            filename = txt_file.name.lower()
            rel_path = txt_file.relative_to(course_dir)
            
            # Extract common fields
            due_match = re.search(due_pattern, text)
            due_str = due_match.group(1).strip() if due_match else None
            due_date = parse_due_date(due_str) if due_str else None
            
            points_match = re.search(points_pattern, text)
            points = points_match.group(1).strip() if points_match else None
            
            # Categorize
            if "ASSIGNMENT:" in text:
                title_match = re.search(r'ASSIGNMENT:\s*(.+?)(?:\n|=)', text)
                content["assignments"].append({
                    "title": title_match.group(1).strip() if title_match else txt_file.stem,
                    "due_date": due_date,
                    "due_str": due_str,
                    "points": points,
                    "content": text,
                    "file": str(rel_path),
                })
            elif "QUIZ:" in text:
                title_match = re.search(r'QUIZ:\s*(.+?)(?:\n|=)', text)
                content["quizzes"].append({
                    "title": title_match.group(1).strip() if title_match else txt_file.stem,
                    "due_date": due_date,
                    "due_str": due_str,
                    "points": points,
                    "content": text,
                    "file": str(rel_path),
                })
            elif "ANNOUNCEMENT:" in text:
                title_match = re.search(r'ANNOUNCEMENT:\s*(.+?)(?:\n|=)', text)
                posted_match = re.search(r'Posted:\s*(.+?)(?:\n|$)', text)
                content["announcements"].append({
                    "title": title_match.group(1).strip() if title_match else txt_file.stem,
                    "posted": posted_match.group(1).strip() if posted_match else None,
                    "content": text,
                    "file": str(rel_path),
                })
            elif "SYLLABUS:" in text:
                content["syllabus"] = {
                    "content": text,
                    "file": str(rel_path),
                }
            elif "reading" in filename or "module" in str(rel_path).lower():
                # Check for chapter references
                chapters = re.findall(chapter_pattern, text.lower())
                if chapters:
                    content["readings"].append({
                        "title": txt_file.stem,
                        "chapters": chapters,
                        "content": text[:500],
                        "file": str(rel_path),
                    })
                else:
                    content["pages"].append({
                        "title": txt_file.stem,
                        "content": text[:500],
                        "file": str(rel_path),
                    })
        except Exception as e:
            continue
    
    return content


def generate_course_briefing(course_dir: Path, week_start: datetime, week_end: datetime) -> dict:
    """Generate briefing for a single course."""
    course_name = get_course_name_short(course_dir.name)
    content = scan_course_content(course_dir)
    now = datetime.now()
    
    # Deduplicate assignments by title + due_date
    all_items = content["assignments"] + content["quizzes"]
    seen = set()
    unique_items = []
    for a in all_items:
        key = (a["title"].lower(), a["due_date"])
        if key not in seen:
            seen.add(key)
            unique_items.append(a)
    
    # Filter to this week's work
    this_week_assignments = []
    overdue_assignments = []
    
    for a in unique_items:
        if a["due_date"]:
            if a["due_date"] < now:
                overdue_assignments.append(a)
            elif week_start <= a["due_date"] <= week_end:
                this_week_assignments.append(a)
    
    # Sort by due date
    this_week_assignments.sort(key=lambda x: x["due_date"])
    overdue_assignments.sort(key=lambda x: x["due_date"])
    
    # Get recent announcements (last 7 days)
    recent_announcements = []
    for ann in content["announcements"]:
        if ann.get("posted"):
            try:
                posted = parse_due_date(ann["posted"])
                if posted and posted >= now - timedelta(days=7):
                    recent_announcements.append(ann)
            except:
                recent_announcements.append(ann)  # Include if can't parse
    
    # Collect readings from this week's assignments
    required_readings = []
    chapter_pattern = r'(?:read|chapter[s]?)\s+(?:chapter[s]?\s+)?(\d+(?:\s*[-–&,]\s*\d+)*)'
    for a in this_week_assignments:
        matches = re.findall(chapter_pattern, a["content"].lower())
        if matches:
            required_readings.extend(matches)
    
    return {
        "course_name": course_name,
        "course_dir": course_dir.name,
        "this_week": this_week_assignments,
        "overdue": overdue_assignments,
        "announcements": recent_announcements,
        "readings": list(set(required_readings)),
        "all_content": content,
    }


def load_study_summaries() -> dict:
    """Load all generated study summaries."""
    summaries = {}
    study_dir = CANVAS_DIR / "_study_summaries"
    
    if study_dir.exists():
        for txt_file in study_dir.rglob("*.txt"):
            try:
                content = txt_file.read_text(encoding='utf-8')
                # Extract chapter info from filename
                name = txt_file.stem.lower()
                summaries[name] = {
                    "file": str(txt_file),
                    "content": content,
                }
            except:
                continue
    
    return summaries


def generate_weekly_briefing() -> str:
    """Generate the complete weekly briefing."""
    now = datetime.now()
    week_start, week_end = get_week_bounds(now)
    
    # Load study summaries
    study_summaries = load_study_summaries()
    
    # Get all course directories
    course_dirs = [d for d in CANVAS_DIR.iterdir() if d.is_dir() and not d.name.startswith("_")]
    
    briefings = []
    for course_dir in sorted(course_dirs):
        briefing = generate_course_briefing(course_dir, week_start, week_end)
        if briefing["this_week"] or briefing["overdue"]:
            briefings.append(briefing)
    
    # Build the output
    output = []
    
    # Header
    output.append("=" * 70)
    output.append("📚 WEEKLY STUDY BRIEFING")
    output.append(f"Week of {week_start.strftime('%B %d')} - {week_end.strftime('%B %d, %Y')}")
    output.append(f"Generated: {now.strftime('%A, %B %d at %I:%M %p')}")
    output.append("=" * 70)
    output.append("")
    
    # Quick Summary
    total_assignments = sum(len(b["this_week"]) for b in briefings)
    total_overdue = sum(len(b["overdue"]) for b in briefings)
    total_points = sum(
        float(a.get("points", 0) or 0) 
        for b in briefings 
        for a in b["this_week"]
    )
    
    output.append("📊 QUICK SUMMARY")
    output.append("-" * 40)
    output.append(f"• Courses with work this week: {len(briefings)}")
    output.append(f"• Assignments due: {total_assignments}")
    output.append(f"• Total points: {total_points:.0f}")
    if total_overdue:
        output.append(f"• ⚠️ OVERDUE items: {total_overdue}")
    output.append("")
    
    # Per-course briefings
    for briefing in briefings:
        output.append("")
        output.append("=" * 70)
        output.append(f"📖 {briefing['course_name'].upper()}")
        output.append("=" * 70)
        
        # Overdue warning
        if briefing["overdue"]:
            output.append("")
            output.append("🚨 OVERDUE - NEEDS IMMEDIATE ATTENTION:")
            for a in briefing["overdue"]:
                days_overdue = (now - a["due_date"]).days
                output.append(f"   • {a['title']}")
                output.append(f"     Was due: {a['due_date'].strftime('%A, %B %d')}")
                output.append(f"     {days_overdue} days overdue!")
        
        # This week's work
        if briefing["this_week"]:
            output.append("")
            output.append("📌 DUE THIS WEEK:")
            
            for a in briefing["this_week"]:
                due_str = a["due_date"].strftime("%A, %B %d at %I:%M %p")
                points_str = f" ({a['points']} points)" if a.get("points") else ""
                
                output.append(f"")
                output.append(f"   📝 {a['title']}{points_str}")
                output.append(f"      Due: {due_str}")
                
                # Extract key instructions from content
                content = a.get("content", "")
                if content:
                    # Look for submission type
                    submission_match = re.search(r'Submission:\s*(.+?)(?:\n|$)', content)
                    if submission_match:
                        output.append(f"      Submit via: {submission_match.group(1)}")
                    
                    # Look for key instructions
                    if "quiz" in a["title"].lower():
                        output.append(f"      📋 This is a QUIZ - study the chapter material!")
        
        # Required readings
        if briefing["readings"]:
            output.append("")
            output.append("📚 REQUIRED READINGS:")
            for chapter in briefing["readings"]:
                output.append(f"   • Chapter {chapter}")
        
        # Recent announcements
        if briefing["announcements"]:
            output.append("")
            output.append("📢 RECENT ANNOUNCEMENTS:")
            for ann in briefing["announcements"][:3]:  # Top 3
                output.append(f"   • {ann['title']}")
        
        # Study recommendations
        output.append("")
        output.append("💡 STUDY RECOMMENDATIONS:")
        
        has_quiz = any("quiz" in a["title"].lower() for a in briefing["this_week"])
        if has_quiz:
            output.append("   • Review lecture materials and chapter readings")
            output.append("   • Focus on key definitions and concepts")
            if briefing["readings"]:
                output.append(f"   • Make sure to complete readings: Chapters {', '.join(briefing['readings'])}")
        
        has_project = any("project" in a["title"].lower() for a in briefing["this_week"])
        if has_project:
            output.append("   • Start the project early - don't wait until the deadline")
            output.append("   • Review assignment requirements carefully")
        
        # Include relevant study summaries
        relevant_summaries = []
        for key, summary in study_summaries.items():
            # Check if this summary matches the course
            course_lower = briefing["course_name"].lower()
            if any(word in key for word in course_lower.split()[:2]):
                relevant_summaries.append(summary)
            # Check for chapter matches
            for ch in briefing["readings"]:
                if f"ch{ch}" in key or f"chapter{ch}" in key:
                    relevant_summaries.append(summary)
        
        if relevant_summaries:
            output.append("")
            output.append("=" * 50)
            output.append("📖 STUDY MATERIALS FOR THIS WEEK")
            output.append("=" * 50)
            for summary in relevant_summaries[:2]:  # Max 2 summaries per course
                output.append("")
                output.append(summary["content"])
    
    # Footer
    output.append("")
    output.append("=" * 70)
    output.append("🎯 ACTION ITEMS FOR THIS WEEK")
    output.append("=" * 70)
    
    action_num = 1
    for briefing in briefings:
        for a in sorted(briefing["this_week"], key=lambda x: x["due_date"]):
            due_str = a["due_date"].strftime("%a %b %d")
            output.append(f"{action_num}. [{due_str}] {a['title']} - {briefing['course_name'][:30]}")
            action_num += 1
    
    output.append("")
    output.append("Good luck with your studies! 📚✨")
    output.append("")
    
    return "\n".join(output)


def save_briefing(briefing_text: str) -> Path:
    """Save the briefing to a file."""
    BRIEFING_DIR.mkdir(parents=True, exist_ok=True)
    
    now = datetime.now()
    week_num = now.isocalendar()[1]
    filename = f"Week_{week_num}_{now.strftime('%Y_%m_%d')}.txt"
    filepath = BRIEFING_DIR / filename
    
    filepath.write_text(briefing_text, encoding='utf-8')
    return filepath


def main():
    import sys
    
    print("""
╔═══════════════════════════════════════════════════════════╗
║           📚 WEEKLY STUDY BRIEFING GENERATOR              ║
╚═══════════════════════════════════════════════════════════╝
    """)
    
    print("🔍 Scanning course content...")
    briefing = generate_weekly_briefing()
    
    # Print to console
    print(briefing)
    
    # Save to file
    filepath = save_briefing(briefing)
    print(f"\n💾 Saved to: {filepath}")
    
    # Audio option
    if "--audio" in sys.argv:
        print("\n🔊 Generating audio version...")
        # TODO: Integrate with TTS
        print("   Audio generation coming soon!")


if __name__ == "__main__":
    main()
