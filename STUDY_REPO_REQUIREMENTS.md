# Study Assistant Repository Requirements

## Important: LLM Usage Policy

**ONLY use Notebook LLM** - Do NOT use:
- ❌ Perplexity API
- ❌ OpenAI API  
- ❌ Eleuther Labs
- ❌ Any other external LLM services

## Allowed Services

✅ **Notebook LLM only** - Local/self-hosted LLM via notebook LLM API

## Why Notebook LLM?

- Privacy: All processing stays local
- Cost: No API costs
- Control: You control the model and data
- No external dependencies: Works offline

## Implementation Example

```python
# notebook_llm_client.py
import httpx
import os

NOTEBOOK_LLM_URL = os.getenv("NOTEBOOK_LLM_URL", "http://localhost:11434")
NOTEBOOK_LLM_MODEL = os.getenv("NOTEBOOK_LLM_MODEL", "llama3.2")

async def generate_with_notebook_llm(prompt: str) -> str:
    """Generate content using notebook LLM only."""
    async with httpx.AsyncClient(timeout=300) as client:
        resp = await client.post(
            f"{NOTEBOOK_LLM_URL}/api/generate",
            json={
                "model": NOTEBOOK_LLM_MODEL,
                "prompt": prompt,
                "stream": False
            }
        )
        
        if resp.status_code == 200:
            return resp.json()["response"]
        else:
            raise Exception(f"Notebook LLM error: {resp.status_code}")
```

## Features to Implement

1. **Course Prioritization**
   - Read from synced Canvas content
   - Prioritize by due dates, importance
   - Use notebook LLM for intelligent prioritization

2. **Book Content Extraction**
   - Extract book references from Canvas content
   - Use notebook LLM to summarize/extract key concepts
   - No external APIs

3. **Podcast Generation**
   - Generate podcast scripts using notebook LLM
   - Use notebook LLM for TTS (if supported) or local TTS
   - Create engaging, study-focused content

## Environment Variables

```env
# Canvas content location (shared with canvas-downloader)
DOWNLOAD_DIR=

# Notebook LLM (ONLY LLM service)
NOTEBOOK_LLM_URL=http://localhost:11434
NOTEBOOK_LLM_MODEL=llama3.2

# Course priorities (optional)
PRIORITY_COURSE_1=COURSE_NAME
```

## Dependencies

```txt
httpx>=0.27.0
python-dotenv>=1.0.0
PyMuPDF>=1.23.0  # For PDF book extraction
python-dateutil>=2.8.0  # For date handling
```

**No OpenAI, Perplexity, or other LLM client libraries needed!**
