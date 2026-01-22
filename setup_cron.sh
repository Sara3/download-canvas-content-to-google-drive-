#!/bin/zsh
# Setup weekly cron job for Canvas sync

PROJECT_DIR="/Users/sara/Desktop/projects/dreamerAgents/canvas-downloader"
PYTHON_PATH=$(which python3)

echo "Setting up weekly Canvas sync cron job..."
echo ""
echo "This will run every Sunday at 2am"
echo "Project directory: $PROJECT_DIR"
echo "Python: $PYTHON_PATH"
echo ""

# Create cron entry
CRON_ENTRY="0 2 * * 0 cd $PROJECT_DIR && $PYTHON_PATH canvas_sync.py >> $PROJECT_DIR/sync.log 2>&1"

# Check if already exists
if crontab -l 2>/dev/null | grep -q "canvas_sync.py"; then
    echo "⚠️  Cron job already exists. Current crontab:"
    crontab -l | grep canvas
    echo ""
    echo "To update it, edit manually: crontab -e"
else
    # Add to crontab
    (crontab -l 2>/dev/null; echo "$CRON_ENTRY") | crontab -
    echo "✅ Cron job added successfully!"
    echo ""
    echo "Current crontab:"
    crontab -l | grep canvas
    echo ""
    echo "To view all cron jobs: crontab -l"
    echo "To remove: crontab -e (then delete the line)"
fi
