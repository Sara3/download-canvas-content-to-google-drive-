#!/bin/zsh
# Canvas Content Sync Script
# Usage: 
#   ./sync.sh              - Incremental sync (only new/changed content)
#   ./sync.sh --force      - Full re-download everything
#   ./sync.sh --course "COURSE NAME"  - Sync specific course only

cd "$(dirname "$0")"

# Activate virtual environment if it exists
if [ -d ".venv" ]; then
    source .venv/bin/activate
elif [ -d "venv" ]; then
    source venv/bin/activate
fi

# Run the sync
if [[ "$1" == "--force" || "$1" == "-f" ]]; then
    echo "🔄 Full sync (re-downloading everything)..."
    python canvas_sync.py --force
elif [[ "$1" == "--course" || "$1" == "-c" ]]; then
    if [ -z "$2" ]; then
        echo "❌ Error: Course name required"
        echo "Usage: ./sync.sh --course \"Course Name\""
        exit 1
    fi
    echo "🔄 Syncing course: $2"
    python canvas_sync.py --course "$2"
elif [[ "$1" == "--help" || "$1" == "-h" ]]; then
    echo "📚 Canvas Content Sync"
    echo ""
    echo "Usage:"
    echo "  ./sync.sh                    Incremental sync (default)"
    echo "  ./sync.sh --force            Full re-download"
    echo "  ./sync.sh --course \"NAME\"   Sync specific course"
    echo ""
    echo "The sync script will:"
    echo "  - Download all content from Canvas courses"
    echo "  - Organize by course and module"
    echo "  - Track downloads to avoid re-downloading unchanged files"
    echo "  - Check for newly released modules weekly"
else
    echo "🔄 Incremental sync (only new/changed content)..."
    python canvas_sync.py
fi
