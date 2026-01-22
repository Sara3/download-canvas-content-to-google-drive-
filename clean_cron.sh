#!/bin/zsh
# Remove all cron jobs except Canvas sync

echo "Current crontab:"
crontab -l
echo ""
echo "Removing all cron jobs except Canvas sync..."
echo ""

# Get current crontab, filter out everything except canvas_sync.py
crontab -l 2>/dev/null | grep -v "firefly111" | grep -v "^#" | grep -v "^$" > /tmp/new_crontab.txt

# Add the Canvas sync job if it's not already there
if ! grep -q "canvas_sync.py" /tmp/new_crontab.txt; then
    echo "0 2 * * 0 cd /Users/sara/Desktop/projects/dreamerAgents/canvas-downloader && /Users/sara/.pyenv/shims/python3 canvas_sync.py >> /Users/sara/Desktop/projects/dreamerAgents/canvas-downloader/sync.log 2>&1" >> /tmp/new_crontab.txt
fi

# Install the cleaned crontab
crontab /tmp/new_crontab.txt

echo "✅ Cleaned crontab:"
crontab -l
echo ""
echo "Only Canvas sync job remains!"
