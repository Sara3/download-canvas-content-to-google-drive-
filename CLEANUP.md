# Cleanup Guide

## Files to KEEP (Core Canvas Sync)

### Essential Files
- `canvas_sync.py` - Main sync script (USE THIS)
- `login_refresh.py` - Session refresh utility
- `sync.sh` - Convenience wrapper script
- `requirements.txt` - Python dependencies
- `README.md` - Updated documentation
- `.env` - Your configuration (keep private)
- `.env.template` - Template for configuration
- `.canvas_session.json` - Session cookies (auto-generated)

### Generated/Log Files (can be cleaned periodically)
- `sync.log` - Sync output log
- `sync_error.log` - Error log
- `download_log.txt` - Old log file
- `download_output.log` - Old log file
- `enrolled_courses.json` - Old cache file
- `login_error.png` - Debug screenshot

## Files to REMOVE or ARCHIVE (Old/Unrelated)

### Old Canvas Downloaders (replaced by canvas_sync.py)
- `canvas_downloader.py` - Old browser-based downloader
- `canvas_api_downloader.py` - Old API downloader
- `download_to_gdrive.py` - Old downloader

### Study Tools (unrelated to sync)
- `study_prep.py` - Study preparation tool
- `study_podcast.py` - Podcast generator
- `weekly_briefing.py` - Weekly briefing generator
- `weekly_prep.py` - Weekly prep tool
- `this_week.py` - This week viewer
- `generate_audio.py` - Audio generation
- `extract_video_links.py` - Old video link extractor

### Cache/Generated
- `__pycache__/` - Python cache (can delete, will regenerate)

## Cleanup Commands

To remove old files:
```bash
# Remove old downloaders
rm canvas_downloader.py canvas_api_downloader.py download_to_gdrive.py

# Remove study tools (if not needed)
rm study_prep.py study_podcast.py weekly_briefing.py weekly_prep.py this_week.py generate_audio.py extract_video_links.py

# Clean Python cache
rm -rf __pycache__

# Clean old logs (optional)
rm download_log.txt download_output.log login_error.png enrolled_courses.json
```

Or archive them:
```bash
mkdir archive
mv canvas_downloader.py canvas_api_downloader.py download_to_gdrive.py archive/
mv study_*.py weekly_*.py this_week.py generate_audio.py extract_video_links.py archive/
```
