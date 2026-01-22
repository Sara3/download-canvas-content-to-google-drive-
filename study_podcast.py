"""
Study Podcast Generator
Creates engaging, story-driven study content with real depth

Features:
- Deep chapter content via Perplexity
- Real-world stories and case studies
- Quiz practice with spaced repetition
- Textbook content extraction
- Honest content - no filler

Usage:
    python study_podcast.py              # Generate this week's podcast
    python study_podcast.py --generate   # Generate audio file
"""

import os
import re
import json
import asyncio
from pathlib import Path
from datetime import datetime, timedelta
from dotenv import load_dotenv
import httpx

# Load .env
SCRIPT_DIR = Path(__file__).parent
load_dotenv(SCRIPT_DIR / ".env")

CANVAS_DIR = Path(os.getenv("DOWNLOAD_DIR", "./canvas_downloads"))
PODCAST_DIR = CANVAS_DIR / "_podcasts"
PERPLEXITY_API_KEY = os.getenv("PERPLEXITY_API_KEY", "")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")


async def get_deep_content(topic: str, context: str, content_type: str = "chapter") -> str:
    """Get deep, substantive content via Perplexity with stories."""
    
    if content_type == "chapter":
        prompt = f"""I'm a college student studying {topic}. 

Context: {context}

Please provide a comprehensive, engaging study guide that includes:

1. **Core Concepts Explained Simply** - Break down the main ideas like you're explaining to a friend
2. **Real-World Story/Case Study** - Give me a compelling real example that illustrates these concepts (a patient case, research study, or real situation)
3. **Key Terms with Memory Hooks** - Important definitions with memorable ways to remember them
4. **Common Exam Questions** - What professors typically ask about this topic
5. **Connections to Daily Life** - How this applies to everyday health decisions

Make it conversational and engaging, like a really good professor explaining things. Include specific facts and numbers where relevant.

Format for audio listening - no tables, no bullet points, just flowing paragraphs that sound natural when read aloud."""

    elif content_type == "story":
        prompt = f"""Tell me a compelling real-world story or case study about {topic}.

This should be:
- A real documented case, research study, or historical example
- Engaging with a narrative arc (situation, challenge, resolution, lesson)
- Educational - illustrating key concepts from {context}
- About 500-800 words
- Suitable for audio listening

Make it feel like a mini-documentary or podcast segment."""

    elif content_type == "quiz":
        prompt = f"""Create an interactive audio quiz about {topic} for a college student.

Format each question like this (for audio):
"Here's a question for you... [pause] What is the definition of [term]? Take a moment to think about it... [pause] The answer is: [definition]. Remember this by thinking about [memory hook]."

Include:
- 5-8 key concept questions
- Mix of definitions, applications, and "why" questions
- Memory hooks and mnemonics
- Brief explanations for each answer

Context: {context}

Make it conversational and encouraging, like a study buddy helping you prepare."""

    if not PERPLEXITY_API_KEY:
        return f"[Content about {topic} would go here - configure PERPLEXITY_API_KEY]"
    
    try:
        async with httpx.AsyncClient(timeout=120) as client:
            resp = await client.post(
                "https://api.perplexity.ai/chat/completions",
                headers={
                    "Authorization": f"Bearer {PERPLEXITY_API_KEY}",
                    "Content-Type": "application/json"
                },
                json={
                    "model": "sonar",
                    "messages": [{"role": "user", "content": prompt}],
                    "temperature": 0.4
                }
            )
            
            if resp.status_code == 200:
                data = resp.json()
                return data["choices"][0]["message"]["content"]
            else:
                print(f"   ⚠️ Perplexity error: {resp.status_code}")
                return ""
    except Exception as e:
        print(f"   ⚠️ Error: {e}")
        return ""


def get_this_week_topics() -> list[dict]:
    """Scan downloaded content to find this week's study topics."""
    from weekly_briefing import get_week_bounds, scan_course_content, get_course_name_short
    
    now = datetime.now()
    week_start, week_end = get_week_bounds(now)
    
    topics = []
    
    # Get all course directories
    course_dirs = [d for d in CANVAS_DIR.iterdir() if d.is_dir() and not d.name.startswith("_")]
    
    for course_dir in sorted(course_dirs):
        course_name = get_course_name_short(course_dir.name)
        content = scan_course_content(course_dir)
        
        # Find this week's assignments
        for a in content["assignments"] + content["quizzes"]:
            if a.get("due_date") and week_start <= a["due_date"] <= week_end:
                topics.append({
                    "course": course_name,
                    "title": a["title"],
                    "type": "assignment" if "ASSIGNMENT" in a.get("content", "") else "quiz",
                    "due_date": a["due_date"],
                    "content": a.get("content", ""),
                })
        
        # Find chapter references in readings
        chapter_pattern = r'chapter[s]?\s*(\d+(?:\s*[-–&,]\s*\d+)*)'
        for reading in content.get("readings", []):
            for match in re.findall(chapter_pattern, reading.get("content", "").lower()):
                topics.append({
                    "course": course_name,
                    "title": f"Chapter {match}",
                    "type": "reading",
                    "content": reading.get("content", ""),
                })
    
    return topics


def extract_textbook_content(course_name: str, chapter: str) -> str:
    """Extract relevant content from downloaded textbook PDFs."""
    # Look for extracted PDF text files
    for pdf_txt in CANVAS_DIR.rglob("*.pdf.txt"):
        content = pdf_txt.read_text(encoding='utf-8', errors='ignore')
        
        # Check if this PDF is from the right course and has the chapter
        if any(word in str(pdf_txt).lower() for word in course_name.lower().split()[:2]):
            # Find chapter section
            chapter_pattern = rf'chapter\s*{chapter}[^\d]'
            if re.search(chapter_pattern, content.lower()):
                # Extract ~2000 chars around the chapter heading
                match = re.search(chapter_pattern, content.lower())
                if match:
                    start = max(0, match.start() - 200)
                    end = min(len(content), match.end() + 3000)
                    return content[start:end]
    
    return ""


async def generate_podcast_script() -> str:
    """Generate the complete podcast script for this week."""
    
    print("📚 Analyzing this week's content...")
    topics = get_this_week_topics()
    
    if not topics:
        return "No topics found for this week."
    
    print(f"   Found {len(topics)} topics to cover")
    
    script = []
    
    # Intro
    now = datetime.now()
    script.append(f"""
Welcome to your weekly study podcast for the week of {now.strftime('%B %d, %Y')}.

This week, we're covering material from {len(set(t['course'] for t in topics))} courses. 
I'll walk you through the key concepts, share some real-world stories to help things stick, 
and we'll do some quiz practice along the way.

Grab your coffee, relax, and let's learn together.
""")
    
    # Group by course
    courses = {}
    for t in topics:
        if t["course"] not in courses:
            courses[t["course"]] = []
        courses[t["course"]].append(t)
    
    # Generate content for each course
    for course_name, course_topics in courses.items():
        script.append(f"\n\n{'='*50}\n")
        script.append(f"Let's start with {course_name}.\n")
        
        # Assignments overview
        assignments = [t for t in course_topics if t["type"] in ["assignment", "quiz"]]
        if assignments:
            script.append(f"\nThis week you have {len(assignments)} items due:\n")
            for a in assignments:
                due = a["due_date"].strftime("%A at %I:%M %p") if a.get("due_date") else "soon"
                script.append(f"- {a['title']}, due {due}\n")
        
        # Deep content for readings/chapters
        readings = [t for t in course_topics if t["type"] == "reading"]
        for reading in readings:
            print(f"   📖 Generating deep content for {course_name} {reading['title']}...")
            
            # Get deep chapter content
            chapter_content = await get_deep_content(
                topic=f"{course_name} - {reading['title']}",
                context=f"College course: {course_name}. This is for {reading['title']} preparation.",
                content_type="chapter"
            )
            
            if chapter_content:
                script.append(f"\n\nNow let's dive into {reading['title']}.\n\n")
                script.append(chapter_content)
            
            # Get a story
            print(f"   📖 Getting real-world story...")
            story = await get_deep_content(
                topic=reading['title'],
                context=course_name,
                content_type="story"
            )
            
            if story:
                script.append("\n\nHere's a real-world example that brings this to life:\n\n")
                script.append(story)
        
        # Quiz practice for quizzes due this week
        quizzes = [t for t in course_topics if t["type"] == "quiz"]
        for quiz in quizzes:
            print(f"   ❓ Generating quiz practice for {quiz['title']}...")
            
            quiz_content = await get_deep_content(
                topic=quiz['title'],
                context=course_name,
                content_type="quiz"
            )
            
            if quiz_content:
                script.append(f"\n\nAlright, let's practice for your upcoming quiz: {quiz['title']}\n\n")
                script.append(quiz_content)
    
    # Spaced repetition summary
    script.append("""

{'='*50}

Before we wrap up, let's do a quick review of the key terms we covered today.
This repetition helps lock things into long-term memory.

""")
    
    # Generate quick review
    all_topics = ", ".join([t['title'] for t in topics])
    review = await get_deep_content(
        topic=f"Quick review of: {all_topics}",
        context="Provide a 5-minute rapid review of the most important terms and concepts, formatted as quick flashcard-style prompts.",
        content_type="quiz"
    )
    if review:
        script.append(review)
    
    # Outro
    script.append("""

That's it for this week's study session! 

Remember:
- You've got this.
- Review these concepts one more time before your quizzes.
- Reach out to your professors during office hours if anything's unclear.

Good luck with your assignments, and I'll see you next week!
""")
    
    return "\n".join(script)


async def generate_audio(script: str, output_path: Path) -> bool:
    """Generate audio using OpenAI TTS."""
    if not OPENAI_API_KEY:
        print("❌ OPENAI_API_KEY not configured")
        return False
    
    # Split into chunks (OpenAI limit is 4096 chars)
    chunks = []
    current_chunk = ""
    
    for paragraph in script.split("\n\n"):
        if len(current_chunk) + len(paragraph) < 4000:
            current_chunk += paragraph + "\n\n"
        else:
            if current_chunk:
                chunks.append(current_chunk.strip())
            current_chunk = paragraph + "\n\n"
    
    if current_chunk:
        chunks.append(current_chunk.strip())
    
    print(f"   {len(chunks)} audio segments to generate")
    
    audio_parts = []
    
    async with httpx.AsyncClient(timeout=120) as client:
        for i, chunk in enumerate(chunks):
            if not chunk.strip():
                continue
                
            print(f"   🎙️ Segment {i+1}/{len(chunks)}...")
            
            # Alternate voices for variety
            voice = "nova" if i % 2 == 0 else "onyx"
            
            try:
                resp = await client.post(
                    "https://api.openai.com/v1/audio/speech",
                    headers={
                        "Authorization": f"Bearer {OPENAI_API_KEY}",
                        "Content-Type": "application/json",
                    },
                    json={
                        "model": "tts-1",
                        "input": chunk[:4096],
                        "voice": voice,
                        "response_format": "mp3",
                    }
                )
                
                if resp.status_code == 200:
                    audio_parts.append(resp.content)
                else:
                    print(f"      ⚠️ Error: {resp.status_code} - {resp.text[:100]}")
                    
            except Exception as e:
                print(f"      ⚠️ Error: {e}")
    
    if not audio_parts:
        return False
    
    # Combine audio
    with open(output_path, 'wb') as f:
        for part in audio_parts:
            f.write(part)
    
    return True


async def main():
    import sys
    
    print("""
╔═══════════════════════════════════════════════════════════╗
║         📚 STUDY PODCAST GENERATOR                         ║
║         Real Content • Stories • No Filler                 ║
╚═══════════════════════════════════════════════════════════╝
    """)
    
    # Generate script
    print("📝 Generating podcast script...")
    script = await generate_podcast_script()
    
    # Save script with clear week naming
    PODCAST_DIR.mkdir(parents=True, exist_ok=True)
    now = datetime.now()
    week_num = now.isocalendar()[1]
    week_start = now - timedelta(days=now.weekday())
    week_label = f"Week_{week_num}_{week_start.strftime('%b_%d')}"
    
    script_path = PODCAST_DIR / f"{week_label}_script.txt"
    script_path.write_text(script, encoding='utf-8')
    
    word_count = len(script.split())
    duration_mins = word_count / 150  # ~150 words per minute spoken
    
    print(f"\n📊 Script Stats:")
    print(f"   Words: {word_count:,}")
    print(f"   Estimated duration: {duration_mins:.0f} minutes")
    print(f"   Saved to: {script_path}")
    
    # Generate audio if requested
    if "--generate" in sys.argv:
        print("\n🎙️ Generating audio...")
        audio_path = PODCAST_DIR / f"{week_label}_podcast.mp3"
        
        success = await generate_audio(script, audio_path)
        
        if success:
            size_mb = audio_path.stat().st_size / (1024 * 1024)
            print(f"\n✅ Podcast generated!")
            print(f"📁 File: {audio_path}")
            print(f"📊 Size: {size_mb:.1f} MB")
            print(f"\n📱 To play on phone:")
            print(f"   Open Google Drive → Canvas/_podcasts/")
        else:
            print("\n❌ Audio generation failed")
    else:
        print(f"\n💡 To generate audio, run:")
        print(f"   python study_podcast.py --generate")


if __name__ == "__main__":
    asyncio.run(main())
