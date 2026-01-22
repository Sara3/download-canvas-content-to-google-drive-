"""
Generate Podcast-Style Study Audio
Creates multi-voice audio from weekly briefing for phone listening

Usage:
    python generate_audio.py                    # Generate from latest briefing
    python generate_audio.py path/to/file.txt   # Generate from specific file
"""

import os
import re
import json
import asyncio
from pathlib import Path
from datetime import datetime
import httpx
from dotenv import load_dotenv

# Load .env
SCRIPT_DIR = Path(__file__).parent
load_dotenv(SCRIPT_DIR / ".env")

CANVAS_DIR = Path(os.getenv("DOWNLOAD_DIR", "./canvas_downloads"))
AUDIO_DIR = CANVAS_DIR / "_audio"
ELEVENLABS_API_KEY = os.getenv("ELLEVEN_LABS_API_KEY", "") or os.getenv("ELEVENLABS_API_KEY", "")


def clean_for_tts(text: str) -> str:
    """Clean text for natural speech synthesis."""
    # Remove markdown formatting
    text = re.sub(r'\*\*([^*]+)\*\*', r'\1', text)  # Bold
    text = re.sub(r'\*([^*]+)\*', r'\1', text)  # Italic
    text = re.sub(r'#{1,6}\s*', '', text)  # Headers
    text = re.sub(r'\[(\d+)\]', '', text)  # Citation numbers
    
    # Remove tables - convert to spoken form
    text = re.sub(r'\|[^\n]+\|', '', text)
    text = re.sub(r'[-=]{3,}', '', text)
    
    # Clean special characters
    text = re.sub(r'[═╔╗╚╝║]', '', text)
    text = re.sub(r'📚|📝|📌|📢|💡|🎯|📖|📊|📅|⭐|❓|🚨|✅', '', text)
    
    # Convert times
    text = re.sub(r'(\d{1,2}):(\d{2})\s*(AM|PM|am|pm)', 
                  lambda m: f"{int(m.group(1))}:{m.group(2)} {m.group(3).upper()}", text)
    
    # Clean up whitespace
    text = re.sub(r'\n{3,}', '\n\n', text)
    text = re.sub(r' {2,}', ' ', text)
    
    return text.strip()


def split_into_segments(text: str) -> list[dict]:
    """Split briefing into segments with voice assignments."""
    segments = []
    
    # Split by sections
    lines = text.split('\n')
    current_section = ""
    current_type = "intro"
    
    for line in lines:
        line = line.strip()
        if not line:
            continue
        
        # Detect section type
        if "WEEKLY STUDY BRIEFING" in line.upper():
            current_type = "intro"
        elif "QUICK SUMMARY" in line.upper():
            current_type = "summary"
        elif "DUE THIS WEEK" in line.upper():
            current_type = "assignments"
        elif "REQUIRED READINGS" in line.upper():
            current_type = "readings"
        elif "ANNOUNCEMENTS" in line.upper():
            current_type = "announcements"
        elif "STUDY RECOMMENDATIONS" in line.upper():
            current_type = "tips"
        elif "STUDY MATERIALS" in line.upper() or "STUDY NOTES" in line.upper():
            current_type = "content"
        elif "KEY TAKEAWAYS" in line.upper():
            current_type = "takeaways"
        elif "DEFINITIONS" in line.upper():
            current_type = "definitions"
        elif "ACTION ITEMS" in line.upper():
            current_type = "actions"
        
        current_section += line + " "
        
        # Create segment at natural breaks
        if len(current_section) > 300 or line.endswith('.') or line.endswith(':'):
            if current_section.strip():
                segments.append({
                    "text": clean_for_tts(current_section.strip()),
                    "type": current_type,
                })
                current_section = ""
    
    # Add remaining
    if current_section.strip():
        segments.append({
            "text": clean_for_tts(current_section.strip()),
            "type": current_type,
        })
    
    return segments


def assign_voices(segments: list[dict]) -> list[dict]:
    """Assign voices based on segment type for podcast feel."""
    voice_map = {
        "intro": "british-female-1",      # Host introduces
        "summary": "british-female-1",    # Host continues
        "assignments": "male-1",          # Co-host for assignments
        "readings": "male-1",             # Co-host
        "announcements": "female-1",      # Different voice for announcements
        "tips": "british-female-1",       # Host for recommendations
        "content": "female-2",            # Study content voice
        "takeaways": "male-2",            # Emphasis voice for key points
        "definitions": "female-2",        # Same as content
        "actions": "british-male-1",      # Authoritative voice for actions
    }
    
    for segment in segments:
        segment["voice"] = voice_map.get(segment["type"], "female-1")
    
    return segments


async def generate_audio_elevenlabs(segments: list[dict], output_path: Path) -> bool:
    """Generate audio using ElevenLabs TTS API."""
    if not ELEVENLABS_API_KEY:
        print("❌ ELEVENLABS_API_KEY not configured in .env")
        return False
    
    # ElevenLabs voice IDs (free tier voices)
    voice_map = {
        "female-1": "21m00Tcm4TlvDq8ikWAM",      # Rachel
        "female-2": "EXAVITQu4vr4xnSDxMaL",      # Bella
        "male-1": "VR6AewLTigWG4xSOukaG",        # Arnold
        "male-2": "pNInz6obpgDQGcFmaJgB",        # Adam
        "british-female-1": "ThT5KcBeYPX3keUQqHPh",  # Dorothy
        "british-male-1": "N2lVS1w4EtoT3dr4eOWO",    # Callum
    }
    
    audio_chunks = []
    
    async with httpx.AsyncClient(timeout=120) as client:
        for i, segment in enumerate(segments):
            if not segment["text"].strip():
                continue
            
            voice_id = voice_map.get(segment["voice"], voice_map["female-1"])
            
            print(f"   🎙️ Generating segment {i+1}/{len(segments)} ({segment['type']})...")
            
            try:
                resp = await client.post(
                    f"https://api.elevenlabs.io/v1/text-to-speech/{voice_id}",
                    headers={
                        "xi-api-key": ELEVENLABS_API_KEY,
                        "Content-Type": "application/json",
                        "Accept": "audio/mpeg",
                    },
                    json={
                        "text": segment["text"][:5000],
                        "model_id": "eleven_multilingual_v2",
                        "voice_settings": {
                            "stability": 0.5,
                            "similarity_boost": 0.75,
                        }
                    }
                )
                
                if resp.status_code == 200:
                    audio_chunks.append(resp.content)
                else:
                    error_msg = resp.text[:200] if resp.text else str(resp.status_code)
                    print(f"      ⚠️ Error: {error_msg}")
                    
            except Exception as e:
                print(f"      ⚠️ Error: {e}")
    
    if not audio_chunks:
        return False
    
    # Combine audio chunks
    with open(output_path, 'wb') as f:
        for chunk in audio_chunks:
            f.write(chunk)
    
    return True


def get_latest_briefing() -> Path | None:
    """Get the most recent weekly briefing file."""
    briefing_dir = CANVAS_DIR / "_weekly_briefings"
    if not briefing_dir.exists():
        return None
    
    briefings = sorted(briefing_dir.glob("*.txt"), reverse=True)
    return briefings[0] if briefings else None


async def main():
    import sys
    
    print("""
╔═══════════════════════════════════════════════════════════╗
║         🎙️ PODCAST-STYLE STUDY AUDIO GENERATOR            ║
╚═══════════════════════════════════════════════════════════╝
    """)
    
    # Get input file
    if len(sys.argv) > 1:
        input_file = Path(sys.argv[1])
    else:
        input_file = get_latest_briefing()
    
    if not input_file or not input_file.exists():
        print("❌ No briefing file found!")
        print("   Run 'python weekly_briefing.py' first")
        return
    
    print(f"📄 Input: {input_file.name}")
    
    # Read and process
    text = input_file.read_text(encoding='utf-8')
    print(f"   {len(text)} characters")
    
    # Split into segments
    print("🔍 Analyzing content...")
    segments = split_into_segments(text)
    segments = assign_voices(segments)
    print(f"   {len(segments)} segments")
    
    # Preview
    print("\n📋 Voice assignments:")
    voice_counts = {}
    for s in segments:
        voice_counts[s["voice"]] = voice_counts.get(s["voice"], 0) + 1
    for voice, count in voice_counts.items():
        print(f"   • {voice}: {count} segments")
    
    # Generate audio
    print("\n🎙️ Generating audio...")
    AUDIO_DIR.mkdir(parents=True, exist_ok=True)
    
    timestamp = datetime.now().strftime("%Y%m%d_%H%M")
    output_file = AUDIO_DIR / f"Study_Podcast_{timestamp}.mp3"
    
    success = await generate_audio_elevenlabs(segments, output_file)
    
    if success:
        size_mb = output_file.stat().st_size / (1024 * 1024)
        print(f"\n✅ Audio generated with ElevenLabs!")
        print(f"📁 Saved to: {output_file}")
        print(f"📊 Size: {size_mb:.1f} MB")
        print(f"\n📱 To play on phone:")
        print(f"   1. Open Google Drive app")
        print(f"   2. Navigate to: Canvas/_audio/")
        print(f"   3. Tap the MP3 file to play")
    else:
        print("\n❌ Audio generation failed")
        print("   Make sure ELLEVEN_LABS_API_KEY is set in .env")


if __name__ == "__main__":
    asyncio.run(main())
