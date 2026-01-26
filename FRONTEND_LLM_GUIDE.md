# Canvas Downloader - Complete System Guide for Frontend LLM

**This is the source of truth for the Canvas Downloader system. All features, file locations, and workflows are documented here.**

## 📋 Table of Contents

1. [System Overview](#system-overview)
2. [Core Features](#core-features)
3. [File Structure & Organization](#file-structure--organization)
4. [Scripts & Commands](#scripts--commands)
5. [Complete Workflow](#complete-workflow)
6. [Where Everything is Saved](#where-everything-is-saved)
7. [API Keys & Configuration](#api-keys--configuration)
8. [Troubleshooting](#troubleshooting)

---

## 🎯 System Overview

This system automatically syncs **ALL content** from SRJC Canvas courses to Google Drive, downloads Zoom recordings, and generates weekly podcasts for offline listening. Everything is organized by course and week for easy access.

**Key Principle:** The system is **incremental** - it only downloads new or changed content, making subsequent runs fast.

---

## ✨ Core Features

### 1. Canvas Content Sync
- **Downloads:** All files, pages, assignments, quizzes, discussions, announcements, syllabus
- **Module-focused:** Prioritizes modules where most content lives
- **Link extraction:** Captures all links from pages, assignments, quizzes, discussions
- **Direct URLs:** Assignments and quizzes include Canvas URLs at the top
- **PowerPoint handling:** Adds Canvas page links for PowerPoints with inline videos
- **Auto-unlock detection:** Automatically syncs newly released modules
- **Incremental sync:** Only downloads new or changed content (tracked by ID + timestamp)

### 2. Weekly Bundles
- **Purpose:** Creates structured weekly exports for all classes
- **Location:** `_weekly/<week-folder>/week.json`
- **Contains:** All assignments, quizzes, prep items, resources, and Zoom links for that week
- **Smart filtering:** Only creates bundles for weeks that have **started** (skips future weeks)

### 3. Zoom LTI Recordings Download
- **Purpose:** Downloads audio-only recordings from Zoom LTI (TechConnect Zoom) for commute listening
- **Format:** Downloads "Audio only" files, optionally converts to MP3
- **Incremental:** Tracks downloaded recordings to avoid re-downloads
- **Location:** `<course-folder>/zoom_recordings/`

### 4. Weekly Podcast Generation
- **Purpose:** Generates audio podcasts from Canvas content for each week
- **Types:**
  - **Per-class podcasts:** One podcast per course per week
  - **Overall podcast:** One weekly overview podcast covering all classes
- **Location:** `_weekly/<week-folder>/podcasts/`
- **Smart filtering:** Only generates podcasts for weeks that have **started** (skips future weeks)

---

## 📁 File Structure & Organization

### Main Directory Structure

```
<DOWNLOAD_DIR>/  (Default: Google Drive/My Drive/Canvas)
│
├── _weekly/                          # Weekly bundles (source of truth for weekly content)
│   ├── 2026-W04_2026-01-19_to_2026-01-25/
│   │   ├── week.json                 # Structured weekly data (assignments, quizzes, resources)
│   │   ├── tasks/                    # Per-task bundle files (self-contained markdown)
│   │   └── podcasts/                 # Generated podcasts for this week
│   │       ├── by_class/
│   │       │   ├── <course-name>/
│   │       │   │   └── <week>__<course>.mp3
│   │       └── overall/
│   │           └── <week>__OVERALL.mp3
│   ├── _index.json                   # Index of all weeks
│   ├── _all_items.json               # All items across all weeks
│   └── _unscheduled.json              # Items without due dates
│
├── <Course Name>/                     # One folder per course
│   ├── modules/
│   │   ├── Module 1 - Introduction/
│   │   │   ├── Lecture Material.txt
│   │   │   ├── lecture_slides.pptx
│   │   │   └── lecture_slides.pptx.canvas_link.txt
│   │   └── Module 2 - Advanced/
│   │       └── _module_locked.txt    # Shows unlock date
│   ├── assignments/
│   │   └── Assignment 1.txt         # Includes Canvas URL at top
│   ├── quizzes/
│   │   └── Quiz 1.txt               # Includes Canvas URL at top
│   ├── pages/
│   ├── announcements/
│   ├── syllabus.txt
│   ├── zoom_recordings/              # Downloaded Zoom audio files
│   │   └── <recording-name>.mp3
│   ├── _sync_state.json              # Tracks what's been synced
│   ├── _manifest.json                # Structured content manifest
│   ├── _all_links.json               # All links from all content
│   ├── _all_links.txt                # Human-readable link list
│   ├── _zoom_links.json              # Dedicated Zoom links list
│   └── _zoom_links.txt               # Human-readable Zoom links
│
└── <Another Course>/
    └── ...
```

### Key Files Explained

#### `_weekly/<week>/week.json`
**Source of truth for weekly content.** Contains:
- Week metadata (start date, end date, week key)
- All items scheduled for that week:
  - Assignments (with due dates)
  - Quizzes (with due dates)
  - Prep items (auto-generated reminders)
  - Resources (readings, links, files)
- Each item includes:
  - `direct_url`: Canvas URL for easy access
  - `local_relative_path`: Where the downloaded file lives
  - `materials`: All related materials for that item
  - `zoom`: Zoom-related links found
  - `task_bundle_relative_path`: Self-contained markdown file

#### `_sync_state.json` (per course)
Tracks every synced item by:
- `item_id`: Unique Canvas ID
- `updated_at`: Last update timestamp
- `file_path`: Where it's saved
- `content_hash`: For change detection

#### `_manifest.json` (per course)
Structured manifest of all course content organized by type.

#### `_zoom_links.json` (per course)
Dedicated list of all Zoom-related links found in the course, including the Zoom LTI portal URL.

---

## 🛠️ Scripts & Commands

### Master Command (Do Everything)

```bash
./sync_all.sh --course "KIN84"
```

**What it does:**
1. ✅ Syncs Canvas content (with weekly bundles)
2. ✅ Downloads Zoom recordings (audio-only, converted to MP3)
3. ✅ Generates weekly podcasts (per-class + overall)

**Options:**
- `./sync_all.sh` - All courses (no Zoom)
- `./sync_all.sh --force` - Force re-download everything
- `./sync_all.sh --no-zoom` - Skip Zoom recordings
- `./sync_all.sh --no-podcast` - Skip podcast generation

### Individual Scripts

#### 1. Canvas Content Sync

```bash
# Incremental sync (only new/changed content)
python canvas_sync.py

# Full sync (re-download everything)
python canvas_sync.py --force

# Sync specific course
python canvas_sync.py --course "KIN84"

# Sync with weekly bundles
python canvas_sync.py --bundle-weeks

# Generate bundles from existing content (no Canvas login)
python canvas_sync.py --bundle-only
```

**What it downloads:**
- All files from modules
- All pages
- All assignments (as .txt with Canvas URL)
- All quizzes (as .txt with Canvas URL)
- All announcements
- Syllabus
- All linked files

**What it creates:**
- Course folders with organized content
- `_sync_state.json` for tracking
- `_manifest.json` for structured data
- `_all_links.json` and `_all_links.txt` for all links
- `_zoom_links.json` and `_zoom_links.txt` for Zoom links
- Weekly bundles in `_weekly/` folder

#### 2. Zoom Recordings Download

```bash
# Download recordings for a course
python zoom_lti_sync.py --course "KIN84"

# With MP3 conversion
python zoom_lti_sync.py --course "KIN84" --convert-mp3

# Dry run (see what would be downloaded)
python zoom_lti_sync.py --course "KIN84" --dry-run

# Limit number of recordings
python zoom_lti_sync.py --course "KIN84" --limit 10
```

**What it does:**
- Uses existing Canvas session (from `login_refresh.py`)
- Navigates to Zoom LTI portal
- Downloads "Audio only" recordings
- Saves to `<course>/zoom_recordings/`
- Tracks downloads to avoid re-downloads

**Requirements:**
- Canvas session must exist (run `login_refresh.py` first)
- `CANVAS_ZOOM_TOOL_URL` in `.env` (optional but recommended)

#### 3. Weekly Podcast Generation

```bash
# Generate podcasts for latest week
python weekly_podcastfy.py --week latest --per-class --overall

# Generate for all weeks
python weekly_podcastfy.py --all-weeks --per-class --overall

# Dry run (see what would be generated)
python weekly_podcastfy.py --week latest --dry-run

# Transcript only (no audio)
python weekly_podcastfy.py --week latest --transcript-only
```

**What it generates:**
- **Per-class podcasts:** One MP3 per course per week
  - Location: `_weekly/<week>/podcasts/by_class/<course-name>/<week>__<course>.mp3`
- **Overall podcast:** One weekly overview
  - Location: `_weekly/<week>/podcasts/overall/<week>__OVERALL.mp3`

**Smart features:**
- Only processes weeks that have **started** (skips future weeks)
- Uses Gemini for transcript generation (free)
- Uses Edge TTS for audio (free, no API key needed)
- Includes all assignments, quizzes, and resources for the week

#### 4. Session Management

```bash
# Create/refresh Canvas session
python login_refresh.py
```

**What it does:**
- Opens browser for Canvas login
- Saves session to `.canvas_session.json`
- Reuses session for all subsequent operations

**When to run:**
- First time setup
- When you see "Session expired" errors
- Periodically to refresh session

---

## 🔄 Complete Workflow

### Initial Setup (One Time)

1. **Install dependencies:**
   ```bash
   pip install -r requirements.txt
   ```

2. **Set up `.env` file:**
   ```bash
   cp .env.template .env
   # Edit .env with your settings
   ```

3. **Create Canvas session:**
   ```bash
   python login_refresh.py
   ```

4. **Optional: Set Zoom tool URL in `.env`:**
   ```bash
   CANVAS_ZOOM_TOOL_URL=https://canvas.santarosa.edu/courses/83136/external_tools/34904
   ```

### Regular Usage (Weekly)

**Recommended: Use the master command**

```bash
./sync_all.sh --course "KIN84"
```

This single command:
1. Syncs all Canvas content for KIN84
2. Creates weekly bundles
3. Downloads new Zoom recordings
4. Generates podcasts for the current week

**For all courses (no Zoom):**

```bash
./sync_all.sh
```

### What Happens Each Run

1. **Canvas Sync:**
   - Checks for new/changed content
   - Downloads only what's new
   - Updates `_sync_state.json`
   - Creates/updates weekly bundles

2. **Zoom Sync (if course specified):**
   - Checks for new recordings
   - Downloads "Audio only" files
   - Converts to MP3 if requested
   - Saves to `<course>/zoom_recordings/`

3. **Podcast Generation:**
   - Reads weekly bundles
   - Generates transcripts using Gemini
   - Converts to audio using Edge TTS
   - Saves to `_weekly/<week>/podcasts/`

---

## 📍 Where Everything is Saved

### Base Directory
**Default:** `/Users/sara/Library/CloudStorage/GoogleDrive-pubandsubs@gmail.com/My Drive/Canvas`

**Can be customized in `.env`:**
```bash
DOWNLOAD_DIR=/path/to/your/canvas/folder
```

### Content Organization

#### Course Content
```
<DOWNLOAD_DIR>/
└── <Course Name>/
    ├── modules/          # All module content
    ├── assignments/      # All assignments
    ├── quizzes/          # All quizzes
    ├── pages/            # All pages
    ├── announcements/     # All announcements
    ├── syllabus.txt       # Course syllabus
    └── zoom_recordings/  # Downloaded Zoom audio files
```

#### Weekly Bundles (Source of Truth)
```
<DOWNLOAD_DIR>/
└── _weekly/
    ├── <week-folder>/    # e.g., "2026-W04_2026-01-19_to_2026-01-25"
    │   ├── week.json     # ⭐ Main weekly data (assignments, quizzes, resources)
    │   ├── tasks/         # Per-task bundle files
    │   └── podcasts/      # Generated podcasts
    ├── _index.json        # Index of all weeks
    ├── _all_items.json    # All items across all weeks
    └── _unscheduled.json  # Items without due dates
```

#### Podcasts
```
<DOWNLOAD_DIR>/
└── _weekly/
    └── <week-folder>/
        └── podcasts/
            ├── by_class/
            │   └── <course-name>/
            │       └── <week>__<course>.mp3
            └── overall/
                └── <week>__OVERALL.mp3
```

#### Zoom Recordings
```
<DOWNLOAD_DIR>/
└── <Course Name>/
    └── zoom_recordings/
        ├── <recording-1>.mp3
        ├── <recording-2>.mp3
        └── _state/
            └── zoom_lti_sync_state.json  # Tracks downloaded recordings
```

### Important Files

- **`_weekly/<week>/week.json`** - ⭐ **Source of truth for weekly content**
- **`<course>/_sync_state.json`** - Tracks synced items (prevents re-downloads)
- **`<course>/_manifest.json`** - Structured course content manifest
- **`<course>/_all_links.json`** - All links found in course
- **`<course>/_zoom_links.json`** - All Zoom links found in course

---

## 🔑 API Keys & Configuration

### Required for Podcast Generation

**Gemini API Key (Free):**
- Get at: https://aistudio.google.com/app/apikey
- Add to `.env`: `GEMINI_API_KEY=your-key-here`
- Used for: Transcript generation

**Edge TTS (Free, No Key Needed):**
- Default TTS model
- No API key required
- Used for: Audio generation

### Optional

**OpenAI API Key:**
- Only needed if using OpenAI TTS model
- Add to `.env`: `OPENAI_API_KEY=your-key-here`
- Not required (Edge TTS is free default)

**ElevenLabs API Key:**
- Only needed if using ElevenLabs TTS model
- Add to `.env`: `ELEVENLABS_API_KEY=your-key-here`
- Not required (Edge TTS is free default)

**Zoom Tool URL:**
- Canvas Zoom external tool page URL
- Add to `.env`: `CANVAS_ZOOM_TOOL_URL=https://canvas.santarosa.edu/courses/.../external_tools/...`
- Helps establish Zoom LTI session

### Environment Variables

**`.env` file structure:**
```bash
# Canvas credentials
CANVAS_STUDENT_ID=878354177
CANVAS_PIN=your_pin

# Download location (auto-detected if empty)
DOWNLOAD_DIR=/path/to/Canvas

# Zoom LTI (optional)
CANVAS_ZOOM_TOOL_URL=https://canvas.santarosa.edu/courses/.../external_tools/...
ZOOM_LTI_ADVANTAGE_URL=https://applications.zoom.us/lti/advantage

# Podcast generation
GEMINI_API_KEY=your-gemini-key
# OPENAI_API_KEY=your-openai-key  # Optional
# ELEVENLABS_API_KEY=your-elevenlabs-key  # Optional
```

---

## 🎯 Key Principles

### 1. Incremental Sync
- **Never re-downloads unchanged content**
- Tracks by item ID + update timestamp
- First run downloads everything; subsequent runs are fast

### 2. Future Week Filtering
- **Only processes weeks that have started**
- Skips future weeks (details not available yet)
- Prevents errors from incomplete data

### 3. Source of Truth
- **`_weekly/<week>/week.json`** is the authoritative weekly data
- Contains all assignments, quizzes, resources, and links
- Used by podcast generation and can be used by frontend UIs

### 4. File Safety
- **Never deletes files**
- Only creates new files or updates changed ones
- Safe to add your own folders (they won't be touched)

### 5. Organization
- **Everything organized by course and week**
- Easy to find content by date or course
- Consistent naming conventions

---

## 🐛 Troubleshooting

### Session Expired
```bash
python login_refresh.py
```

### No Week Folders
```bash
python canvas_sync.py --bundle-weeks
```

### Podcasts Not Generating
1. Check API keys in `.env`
2. Verify week folders exist: `ls _weekly/`
3. Check if weeks are in the future (they'll be skipped)
4. Run with `--dry-run` to see what would be generated

### Zoom Recordings Not Downloading
1. Verify `CANVAS_ZOOM_TOOL_URL` in `.env`
2. Check that session exists: `python login_refresh.py`
3. Run without `--headless` to see what's happening
4. Check `zoom_recordings/_state/` for tracking info

### Google Drive Not Found
1. Make sure Google Drive desktop app is installed
2. Set `DOWNLOAD_DIR` manually in `.env`
3. Check path in `.env` is correct

---

## 📊 Data Flow

```
Canvas API
    ↓
canvas_sync.py
    ↓
Course Folders + _weekly/ bundles
    ↓
weekly_podcastfy.py
    ↓
Podcasts in _weekly/<week>/podcasts/
```

```
Canvas → Zoom LTI Portal
    ↓
zoom_lti_sync.py
    ↓
<course>/zoom_recordings/
```

---

## 🎓 For Frontend LLM

**When helping users with this system:**

1. **Always reference `_weekly/<week>/week.json`** as the source of truth for weekly content
2. **Check `_sync_state.json`** to see what's been synced
3. **Look in `_weekly/`** for weekly bundles and podcasts
4. **Check course folders** for downloaded content
5. **Remember:** Future weeks are automatically skipped
6. **Use `./sync_all.sh --course "COURSE"`** as the recommended command

**Key locations to reference:**
- Weekly data: `_weekly/<week>/week.json`
- Podcasts: `_weekly/<week>/podcasts/`
- Zoom recordings: `<course>/zoom_recordings/`
- Course content: `<course>/modules/`, `<course>/assignments/`, etc.

**Common user questions:**
- "Where are my podcasts?" → `_weekly/<week>/podcasts/`
- "Where is this week's content?" → `_weekly/<week>/week.json`
- "How do I sync everything?" → `./sync_all.sh --course "COURSE"`
- "Why aren't future weeks showing?" → They're automatically skipped (by design)

---

**Last Updated:** 2026-01-26
**Version:** 1.0
**Maintainer:** Canvas Downloader System
