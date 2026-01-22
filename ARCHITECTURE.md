# Recommended Architecture: Separate Repositories

## Current Situation
- **canvas-downloader**: Core sync utility (focused, maintainable)
- **Study/Podcast tools**: Higher-level features that depend on synced content

## Recommended Approach: Two Repositories

### 1. `canvas-downloader` (Current Repo)
**Purpose**: Core Canvas content sync utility
- ✅ Keep this focused on syncing Canvas content
- ✅ No LLM dependencies
- ✅ No podcast generation
- ✅ Simple, reliable, maintainable

### 2. `canvas-study-assistant` (New Repo)
**Purpose**: Study tools, prioritization, and podcast generation
- Course work prioritization
- Book content extraction
- Podcast generation with **notebook LLM ONLY** (no Perplexity, OpenAI, etc.)
- Weekly study briefings
- Study summaries

**Important**: Uses **notebook LLM only** - no external LLM APIs

## Integration Options

### Option A: Git Submodule (Recommended)
```bash
# In new repo
git submodule add https://github.com/Sara3/download-canvas-content-to-google-drive-.git canvas-sync
```

**Pros:**
- Clear dependency
- Can update canvas-sync independently
- Version control for dependency

### Option B: Python Package Import
```python
# In new repo, add canvas-downloader as dependency
import sys
sys.path.append('../canvas-downloader')
from canvas_sync import CanvasSync
```

**Pros:**
- Simple
- Direct import

### Option C: Separate Services (RECOMMENDED)
- Canvas sync runs independently (cron)
- Study tools read from synced content directory
- No code dependency
- Both use same `DOWNLOAD_DIR` environment variable

**Pros:**
- Complete separation
- Simplest approach - no submodules or imports needed
- Can use different languages/tech stacks
- Zero setup - just read files from shared directory

## Recommended Structure for New Repo

```
canvas-study-assistant/
├── README.md
├── requirements.txt
├── .env.template
├── src/
│   ├── prioritizer.py          # Course work prioritization
│   ├── book_extractor.py       # Extract content from books
│   ├── podcast_generator.py    # Generate podcasts with notebook LLM
│   ├── weekly_planner.py       # Weekly study planning
│   └── notebook_llm_client.py  # Notebook LLM integration
└── config/
    └── course_config.json      # Course priorities, book mappings

# No submodule needed - reads from shared DOWNLOAD_DIR
```

## Benefits of Separation

1. **Maintainability**: Each repo has clear purpose
2. **Reusability**: Canvas sync can be used by other projects
3. **Testing**: Easier to test each component
4. **Deployment**: Can deploy/update independently
5. **Dependencies**: Study tools can have heavy LLM deps without bloating sync tool

## Migration Path

1. Create new repo: `canvas-study-assistant`
2. Add canvas-downloader as submodule or dependency
3. Move/refactor study tools to new repo
4. Integrate notebook LLM
5. Add prioritization logic
6. Add book extraction features
