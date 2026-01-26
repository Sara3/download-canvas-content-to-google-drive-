# Canvas Content Sync for SRJC

Automatically syncs ALL content from your SRJC Canvas courses to Google Drive with intelligent tracking to avoid re-downloads.

## Features

- 📦 **Module-focused**: Prioritizes modules where most content lives
- 🔄 **Incremental sync**: Only downloads new or changed content
- 🔗 **Link extraction**: Captures all links from pages, assignments, quizzes, discussions
- 📄 **Direct URLs**: Assignments and quizzes include Canvas URLs for easy access
- 🎥 **PowerPoint handling**: Adds Canvas page links for PowerPoints with inline videos
- 🔓 **Auto-unlock detection**: Automatically syncs newly released modules
- 📊 **Comprehensive tracking**: Prevents re-downloading unchanged files

## Quick Start

### 1. Install Dependencies

```bash
pip install -r requirements.txt
```

#### Podcast generation prerequisites
If you want to generate weekly podcasts using [Podcastfy](https://github.com/souzatharsis/podcastfy?tab=readme-ov-file), you also need `ffmpeg` installed (Podcastfy uses it for audio processing).

On macOS:

```bash
brew install ffmpeg
```

### 2. Set Up Session

First, you need to log in to Canvas to create a session:

```bash
python login_refresh.py
```

This will open a browser window. Log in to Canvas, and the session will be saved automatically.

### 3. Run Sync

**Incremental sync** (only new/changed content):
```bash
python canvas_sync.py
```

**Full sync** (re-download everything):
```bash
python canvas_sync.py --force
```

**Sync specific course**:
```bash
python canvas_sync.py --course "NUTR 10"
```

Or use the convenience script:
```bash
./sync.sh              # Incremental sync
./sync.sh --force      # Full sync
./sync.sh --course "NUTR 10"  # Specific course
```

### 🚀 Do Everything (Recommended)

**One command to sync Canvas, download Zoom recordings, and generate podcasts:**

```bash
./sync_all.sh --course "KIN84"
```

This runs:
1. ✅ Canvas content sync (with weekly bundles)
2. ✅ Zoom recordings download (audio-only, converted to MP3)
3. ✅ Weekly podcast generation (per-class + overall)

**For all courses** (no Zoom):
```bash
./sync_all.sh
```

**Options:**
- `--course "KIN84"` - Sync specific course + Zoom recordings
- `--force` - Force re-download everything
- `--no-zoom` - Skip Zoom recordings
- `--no-podcast` - Skip podcast generation

## What Gets Downloaded

Content is organized by course in your Google Drive:

```
Google Drive/My Drive/Canvas/
├── NUTR 10 - Introduction to Nutrition/
│   ├── modules/
│   │   ├── Module 1 - Introduction/
│   │   │   ├── Lecture Material.txt
│   │   │   ├── lecture_slides.pptx
│   │   │   ├── lecture_slides.pptx.canvas_link.txt  ← For inline videos
│   │   │   └── ...
│   │   ├── Module 2 - Advanced Topics/
│   │   │   └── _module_locked.txt  ← Shows unlock date
│   │   └── ...
│   ├── assignments/
│   │   ├── Assignment 1.txt  ← Includes Canvas URL at top
│   │   └── ...
│   ├── quizzes/
│   │   ├── Quiz 1.txt  ← Includes Canvas URL at top
│   │   └── ...
│   ├── pages/
│   ├── announcements/
│   ├── syllabus.txt
│   ├── _sync_state.json      ← Tracks what's been synced
│   ├── _manifest.json         ← Structured content manifest
│   └── _all_links.json        ← All links from all content
└── Another Course/
    └── ...
```

## Key Features

### Direct Canvas URLs
- **Assignments & Quizzes**: Each file includes the Canvas URL at the top for easy access
- **PowerPoints**: Companion `.canvas_link.txt` or `.page_link.txt` files for accessing inline videos

### Weekly Bundles (ALL classes, source-of-truth export)
If you want a **single, weekly “batch”** you can feed into another client (which will then filter/sort), the sync can generate a `_weekly/` folder containing **one folder per ISO week** with a `week.json` inside.

Generate weekly bundles while syncing:

```bash
python canvas_sync.py --bundle-weeks
```

Or generate bundles from existing `_manifest.json` files (no Canvas login required):

```bash
python canvas_sync.py --bundle-only
```

Output:

```
Google Drive/My Drive/Canvas/
└── _weekly/
    ├── 2026-W05_2026-01-26_to_2026-02-01/
    │   └── week.json
    ├── _index.json
    ├── _all_items.json
    └── _unscheduled.json
```

What gets included:
- **Assignments & quizzes** (scheduled by `due_at`)
- **Prep items** (auto-generated: assignments \(3 days before\), quizzes \(2 days before\))
- **Prep resources with direct links** (auto-included as `kind: "resource"`):
  - Links referenced inside assignment/quiz descriptions (articles, references, videos, PDFs, etc.)
  - Content from the same module folder (pages/files/external links) when the graded item lives under `modules/<...>/`

Each *task* item (`kind` in `assignment|quiz|prep`) also includes:
- `materials`: structured list of everything referenced + in the same module folder (with `local_relative_path` when available)
- `zoom`: best-effort list of Zoom-related links found in the task/module
- `priority_hint`: simple `"high" | "medium" | "low"` hint (e.g. “participation” is de-emphasized vs graded work)
- `task_bundle_relative_path`: a generated self-contained markdown file under `_weekly/<week>/tasks/...` that includes instructions/context + all materials (so UIs don’t have to rely on deep links)

Each item includes:
- `direct_url`: stable URL you can open in Canvas (or the external site)
- `local_relative_path`: where the downloaded `.txt`/file lives in your Canvas drive folder (when available)

If an item has no due/unlock date, it goes into `_unscheduled.json`.

### Weekly podcasts (per class + overall)
Once you have weekly bundles, you can generate:
- **One podcast per class per week**
- **One overall “this week” podcast**

```bash
python weekly_podcastfy.py --week latest --per-class --overall
```

Dry-run (see what sources will be used without calling the LLM/TTS):

```bash
python weekly_podcastfy.py --week latest --dry-run
```

Outputs are saved into the week folder under:
`Canvas/_weekly/<week-folder>/podcasts/`

### Link Extraction
- Extracts all links from pages, assignments, quizzes, and discussions
- Downloads linked files automatically
- Saves comprehensive link lists in `_all_links.json` and `_all_links.txt`

### Module Release Detection
- Tracks locked/unreleased modules
- Automatically detects and syncs newly released modules
- Shows unlock dates for future modules

### Incremental Sync
- Tracks every item by ID and update timestamp
- Only downloads new or changed content
- First run downloads everything; subsequent runs are fast

## Weekly Automated Sync

### Option 1: Cron (Simple)

Edit your crontab:
```bash
crontab -e
```

Add this line (update the path to your project):
```bash
# Run every Sunday at 2am
0 2 * * 0 cd /Users/sara/Desktop/projects/dreamerAgents/canvas-downloader && /usr/bin/python3 canvas_sync.py >> sync.log 2>&1
```

### Option 2: launchd (More Reliable on macOS)

Create `~/Library/LaunchAgents/com.canvas.sync.plist`:

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.canvas.sync</string>
    <key>ProgramArguments</key>
    <array>
        <string>/usr/bin/python3</string>
        <string>/Users/sara/Desktop/projects/dreamerAgents/canvas-downloader/canvas_sync.py</string>
    </array>
    <key>StartCalendarInterval</key>
    <dict>
        <key>Weekday</key>
        <integer>0</integer>
        <key>Hour</key>
        <integer>2</integer>
    </dict>
    <key>WorkingDirectory</key>
    <string>/Users/sara/Desktop/projects/dreamerAgents/canvas-downloader</string>
    <key>StandardOutPath</key>
    <string>/Users/sara/Desktop/projects/dreamerAgents/canvas-downloader/sync.log</string>
    <key>StandardErrorPath</key>
    <string>/Users/sara/Desktop/projects/dreamerAgents/canvas-downloader/sync_error.log</string>
</dict>
</plist>
```

Load it:
```bash
launchctl load ~/Library/LaunchAgents/com.canvas.sync.plist
```

Check status:
```bash
launchctl list | grep canvas
```

## Troubleshooting

### Session Expired
If you see "Session expired" errors:
```bash
python login_refresh.py
```
Log in again in the browser window.

### No Courses Found
- Check that you're enrolled in active courses
- Verify your session is valid: `python login_refresh.py`
- Try running with `--force` to see more details

### Google Drive Not Found
The script auto-detects Google Drive on Mac. If it doesn't work:
1. Make sure Google Drive desktop app is installed
2. Set manual path in `.env`: `DOWNLOAD_DIR=/path/to/Google Drive/My Drive/Canvas`

### Module Not Syncing
- Locked modules are tracked but not synced until they unlock
- Check `_module_locked.txt` files for unlock dates
- Newly released modules are automatically detected on next sync

## What It Doesn't Download

- DRM-protected content (McGraw-Hill Connect, etc.)
- Quiz questions from quizzes you haven't completed
- External tool content requiring separate login (but see Zoom note below)
- Proctored exam content

## Zoom LTI “Cloud Recordings” (KIN84 / TechConnect Zoom)

Canvas external tools (like Zoom LTI) aren’t downloadable via the Canvas API, but this repo includes a **best-effort** downloader for the Zoom LTI portal so you can save **audio-only recordings** for commute listening.

### 1) Make sure your Canvas session exists

```bash
python login_refresh.py
```

### 2) Set your Canvas Zoom tool URL

In `.env`, set the course’s Zoom external tool page URL (from Canvas course navigation), e.g.:

```bash
CANVAS_ZOOM_TOOL_URL=https://canvas.santarosa.edu/courses/83136/external_tools/34904
```

### 3) Download recordings

```bash
python zoom_lti_sync.py --course "KIN84"
```

Downloads go to:
`<DOWNLOAD_DIR>/<course>/zoom_recordings/`

Optional: convert to MP3 (requires `ffmpeg`):

```bash
python zoom_lti_sync.py --course "KIN84" --convert-mp3
```

## File Safety

**The sync script never deletes files.** It only:
- Creates new files
- Updates changed files
- Skips unchanged files

You can safely add your own folders (like a `books` folder) - they will never be touched.

## Notes

- First run may take 10-30 minutes depending on course content
- Subsequent runs are much faster (only new/changed content)
- All credentials stay local - nothing is sent anywhere except Canvas
- Videos can be large - ensure you have enough Google Drive space
- The script tracks everything in `_sync_state.json` per course
