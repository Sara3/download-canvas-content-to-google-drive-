"""
WEEKLY PREP - One Command Does Everything
==========================================

This is the ONLY script you need to run each week.
It does everything in order:

1. Syncs new content from Canvas (incremental)
2. Extracts lecture/video links
3. Generates study summaries for required readings
4. Creates weekly briefing
5. Generates podcast audio

Usage:
    python weekly_prep.py           # Full weekly prep
    python weekly_prep.py --quick   # Skip audio generation (faster)

Run this every Friday morning before your commute!
"""

import os
import sys
import asyncio
from pathlib import Path
from datetime import datetime, timedelta
from dotenv import load_dotenv

# Load environment
SCRIPT_DIR = Path(__file__).parent
load_dotenv(SCRIPT_DIR / ".env")

CANVAS_DIR = Path(os.getenv("DOWNLOAD_DIR", "./canvas_downloads"))


def get_week_info():
    """Get current week info for naming."""
    now = datetime.now()
    week_num = now.isocalendar()[1]
    week_start = now - timedelta(days=now.weekday())
    week_end = week_start + timedelta(days=6)
    return {
        "week_num": week_num,
        "year": now.year,
        "start": week_start,
        "end": week_end,
        "label": f"Week_{week_num}_{week_start.strftime('%b_%d')}",
    }


async def run_step(step_name: str, command: str) -> bool:
    """Run a step and show progress."""
    print(f"\n{'='*60}")
    print(f"📌 STEP: {step_name}")
    print(f"{'='*60}\n")
    
    proc = await asyncio.create_subprocess_shell(
        command,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        cwd=str(SCRIPT_DIR),
    )
    
    while True:
        line = await proc.stdout.readline()
        if not line:
            break
        print(line.decode().rstrip())
    
    await proc.wait()
    return proc.returncode == 0


async def main():
    week = get_week_info()
    skip_audio = "--quick" in sys.argv
    
    print(f"""
╔══════════════════════════════════════════════════════════════╗
║                    📚 WEEKLY PREP                             ║
║                                                               ║
║   {week['label'].replace('_', ' '):^55} ║
║   {week['start'].strftime('%B %d')} - {week['end'].strftime('%B %d, %Y'):^45} ║
╚══════════════════════════════════════════════════════════════╝
    """)
    
    # Step 1: Sync Canvas
    print("\n⏳ This may take a few minutes...\n")
    
    await run_step(
        "1/6 - Sync Canvas Content (incremental)",
        "source .venv/bin/activate && python canvas_api_downloader.py"
    )
    
    # Step 2: Extract video/lecture links
    await run_step(
        "2/6 - Extract Video & Lecture Links",
        "source .venv/bin/activate && python extract_video_links.py"
    )
    
    # Step 3: Extract PDF text from any new PDFs
    await run_step(
        "3/6 - Extract Text from PDFs",
        """source .venv/bin/activate && python -c "
import asyncio
from canvas_api_downloader import CanvasAPIDownloader, DOWNLOAD_DIR
async def extract():
    d = CanvasAPIDownloader()
    for course_dir in DOWNLOAD_DIR.iterdir():
        if course_dir.is_dir() and not course_dir.name.startswith('_'):
            await d.extract_all_pdfs(course_dir)
asyncio.run(extract())
print('✅ PDF extraction complete')
"
"""
    )
    
    # Step 4: Generate study summaries for this week's readings
    await run_step(
        "4/6 - Generate Study Summaries (via Perplexity)",
        "source .venv/bin/activate && python study_prep.py --scan"
    )
    
    # Step 5: Create weekly briefing
    await run_step(
        "5/6 - Create Weekly Briefing",
        "source .venv/bin/activate && python weekly_briefing.py"
    )
    
    # Step 6: Generate podcast audio
    if not skip_audio:
        await run_step(
            "6/6 - Generate Podcast Audio (this takes a while)",
            "source .venv/bin/activate && python study_podcast.py --generate"
        )
    else:
        print("\n⏭️ Skipping audio generation (--quick mode)")
    
    # Summary
    print(f"""

╔══════════════════════════════════════════════════════════════╗
║                    ✅ WEEKLY PREP COMPLETE                    ║
╚══════════════════════════════════════════════════════════════╝

📂 Your files are in Google Drive:
   Canvas/_weekly_briefings/{week['label']}.txt
   Canvas/_podcasts/{week['label']}_podcast.mp3
   Canvas/_lecture_links.txt          ← All video/Zoom links!
   Canvas/_study_summaries/

📱 To listen/watch on your phone:
   1. Open Google Drive app
   2. Go to Canvas/_podcasts/ for audio
   3. Open _lecture_links.txt for Zoom/YouTube links

📅 This covers: {week['start'].strftime('%B %d')} - {week['end'].strftime('%B %d')}

🎯 Your assignments this week are in the briefing.
   Good luck! 📚
""")


if __name__ == "__main__":
    asyncio.run(main())
