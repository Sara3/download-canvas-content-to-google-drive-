#!/bin/zsh
# Master sync script - does everything:
# 1. Sync Canvas content (with weekly bundles)
# 2. Download Zoom recordings (if course specified)
# 3. Generate weekly podcasts
#
# Usage:
#   ./sync_all.sh                    # All courses, no Zoom
#   ./sync_all.sh --course "KIN84"   # Specific course + Zoom recordings
#   ./sync_all.sh --force            # Force re-download everything

cd "$(dirname "$0")"

# Activate virtual environment if it exists
if [ -d ".venv" ]; then
    source .venv/bin/activate
elif [ -d "venv" ]; then
    source venv/bin/activate
fi

COURSE=""
FORCE_FLAG=""
ZOOM_SYNC=true
PODCAST=true

# Parse arguments
while [[ $# -gt 0 ]]; do
    case $1 in
        --course|-c)
            COURSE="$2"
            shift 2
            ;;
        --force|-f)
            FORCE_FLAG="--force"
            shift
            ;;
        --no-zoom)
            ZOOM_SYNC=false
            shift
            ;;
        --no-podcast)
            PODCAST=false
            shift
            ;;
        --help|-h)
            echo "📚 Complete Canvas Sync (Everything)"
            echo ""
            echo "Usage:"
            echo "  ./sync_all.sh                    Sync all courses + generate podcasts"
            echo "  ./sync_all.sh --course KIN84     Sync specific course + Zoom + podcasts"
            echo "  ./sync_all.sh --force           Force re-download everything"
            echo "  ./sync_all.sh --no-zoom         Skip Zoom recordings download"
            echo "  ./sync_all.sh --no-podcast      Skip podcast generation"
            echo ""
            exit 0
            ;;
        *)
            echo "Unknown option: $1"
            echo "Run ./sync_all.sh --help for usage"
            exit 1
            ;;
    esac
done

echo "╔═══════════════════════════════════════════════════════════╗"
echo "║         Complete Canvas Sync (Everything)                 ║"
echo "╚═══════════════════════════════════════════════════════════╝"
echo ""

# Step 1: Canvas sync (with weekly bundles)
echo "📦 Step 1/3: Syncing Canvas content..."
if [ -n "$COURSE" ]; then
    python canvas_sync.py --course "$COURSE" --bundle-weeks $FORCE_FLAG
else
    python canvas_sync.py --bundle-weeks $FORCE_FLAG
fi

if [ $? -ne 0 ]; then
    echo "❌ Canvas sync failed. Stopping."
    exit 1
fi
echo "✅ Canvas sync complete"
echo ""

# Step 2: Zoom recordings (if course specified)
if [ "$ZOOM_SYNC" = true ] && [ -n "$COURSE" ]; then
    echo "🎥 Step 2/3: Downloading Zoom recordings for $COURSE..."
    python zoom_lti_sync.py --course "$COURSE" --convert-mp3 2>&1 | grep -v "^$" || {
        echo "⚠️  Zoom sync had issues (this is okay if no recordings exist yet)"
    }
    echo "✅ Zoom sync complete"
    echo ""
elif [ "$ZOOM_SYNC" = true ]; then
    echo "⏭️  Step 2/3: Skipping Zoom (no course specified - use --course)"
    echo ""
fi

# Step 3: Generate podcasts
if [ "$PODCAST" = true ]; then
    echo "🎙️  Step 3/3: Generating weekly podcasts..."
    # Use edge TTS (free, no API key needed) instead of openai
    python weekly_podcastfy.py --week latest --per-class --overall --tts-model edge 2>&1 | grep -v "^$" || {
        echo "⚠️  Podcast generation had issues (check API keys in .env)"
    }
    echo "✅ Podcast generation complete"
    echo ""
else
    echo "⏭️  Step 3/3: Skipping podcast generation (--no-podcast)"
    echo ""
fi

echo "╔═══════════════════════════════════════════════════════════╗"
echo "║                    ✅ All Done!                            ║"
echo "╚═══════════════════════════════════════════════════════════╝"
echo ""
echo "📂 Content saved to your Canvas download directory"
if [ "$PODCAST" = true ]; then
    echo "🎙️  Podcasts saved to: <Canvas>/_weekly/<week>/podcasts/"
fi
