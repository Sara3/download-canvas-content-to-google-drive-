# Setup Guide for New Study Assistant Repository

## Quick Start

### 1. Create New Repository on GitHub
```bash
# Create repo: canvas-study-assistant (or your preferred name)
```

### 2. Clone and Setup
```bash
cd /Users/sara/Desktop/projects/dreamerAgents
git clone https://github.com/Sara3/canvas-study-assistant.git
cd canvas-study-assistant
```

### 3. Add Canvas Sync as Submodule
```bash
git submodule add https://github.com/Sara3/download-canvas-content-to-google-drive-.git canvas-sync
git commit -m "Add canvas-sync as submodule"
```

### 4. Initial Structure
```bash
mkdir -p src config
touch README.md requirements.txt .env.template
```

### 5. Example requirements.txt
```txt
# Core dependencies
httpx>=0.27.0
python-dotenv>=1.0.0

# LLM/Notebook integration
# Use notebook LLM only - no Perplexity, OpenAI, or Eleuther Labs
# notebook-llm-client or direct HTTP client for notebook LLM API
httpx>=0.27.0  # For notebook LLM API calls

# PDF processing (for book extraction)
PyMuPDF>=1.23.0
pdfplumber>=0.10.0

# Course prioritization
python-dateutil>=2.8.0
```

### 6. Example .env.template
```env
# Canvas sync (uses same session)
DOWNLOAD_DIR=

# Notebook LLM (ONLY - no other LLM services)
NOTEBOOK_LLM_URL=http://localhost:11434
NOTEBOOK_LLM_MODEL=llama3.2
# Do NOT use: OPENAI_API_KEY, PERPLEXITY_API_KEY, or any other LLM services

# Course priorities (optional)
PRIORITY_COURSE_1=COURSE_NAME
PRIORITY_COURSE_2=COURSE_NAME
```

## Integration Example

### Using Canvas Sync
```python
# src/prioritizer.py
import sys
from pathlib import Path

# Add canvas-sync to path
sys.path.insert(0, str(Path(__file__).parent.parent / "canvas-sync"))
from canvas_sync import CanvasSync

async def sync_and_prioritize():
    # Sync content
    syncer = CanvasSync()
    await syncer.run()
    
    # Then prioritize
    # ... your prioritization logic
```

### Reading Synced Content
```python
# src/book_extractor.py
from pathlib import Path
import os

CANVAS_DIR = Path(os.getenv("DOWNLOAD_DIR", "./canvas_downloads"))

def get_course_content(course_name: str):
    course_dir = CANVAS_DIR / course_name
    # Read from synced content
    # Extract book references
    # ...
```

## Benefits

✅ **Separation of Concerns**: Sync vs Study tools
✅ **Independent Updates**: Update each repo separately  
✅ **Clear Dependencies**: Study tools depend on synced content
✅ **Easier Testing**: Test each component independently
✅ **Better Organization**: Each repo has single responsibility
