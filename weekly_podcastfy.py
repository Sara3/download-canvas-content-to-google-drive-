#!/usr/bin/env python3
"""
Weekly Podcast Generator (Podcastfy)

Reads Canvas weekly bundles under:
  <Canvas DOWNLOAD_DIR>/_weekly/<week-folder>/week.json

Then generates:
- One podcast per class (course) for the week
- One overall “this week” podcast (all classes)

This uses Podcastfy:
  https://github.com/souzatharsis/podcastfy?tab=readme-ov-file

Notes:
- Canvas URLs usually require authentication, so we primarily feed Podcastfy local files that
  were downloaded by `canvas_sync.py` (assignment/quiz .txt, PDFs, etc.), plus external URLs.
- Generated MP3s are saved inside each week folder under `podcasts/`.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any, Iterable

from dotenv import load_dotenv


CANVAS_HOST = "canvas.santarosa.edu"


def _normalize_podcast_env() -> None:
    """Map common key typos/aliases to Podcastfy's expected env vars."""
    aliases = {
        # OpenAI
        "OPENAI_API_KEY": [
            "OPEN_AI_API_KEY",
            "OPENAI_KEY",
            "OPEN_AI_APIA_KEY",  # common typo seen in this repo
            "OPEN_API_KEY",
        ],
        # ElevenLabs
        "ELEVENLABS_API_KEY": [
            "ELEVEN_LABS_API_KEY",
            "ELLEVEN_LABS_API_KEY",  # common typo seen in this repo
            "ELEVENLABS_KEY",
        ],
        # Gemini
        "GEMINI_API_KEY": [
            "GOOGLE_API_KEY",
            "GOOGLE_GENAI_API_KEY",
        ],
    }

    for canonical, candidates in aliases.items():
        if os.getenv(canonical):
            continue
        for c in candidates:
            v = os.getenv(c)
            if v:
                os.environ[canonical] = v
                break


def _default_llm_config() -> tuple[str, str]:
    """
    Choose an LLM backend for Podcastfy transcript generation.

    Podcastfy defaults to Gemini; if you don't have `GEMINI_API_KEY`, we fall back to OpenAI via LiteLLM.
    """
    if os.getenv("PODCASTFY_LLM_MODEL") and os.getenv("PODCASTFY_LLM_API_KEY_LABEL"):
        return os.environ["PODCASTFY_LLM_MODEL"], os.environ["PODCASTFY_LLM_API_KEY_LABEL"]

    if os.getenv("GEMINI_API_KEY"):
        return "gemini-2.5-flash", "GEMINI_API_KEY"

    if os.getenv("OPENAI_API_KEY"):
        # LiteLLM / ChatLiteLLM accepts OpenAI model names directly.
        return "gpt-4o-mini", "OPENAI_API_KEY"

    raise RuntimeError(
        "No transcript-generation API key found. Set GEMINI_API_KEY (recommended) "
        "or OPENAI_API_KEY in your .env."
    )


def _sanitize_filename(name: str, max_len: int = 120) -> str:
    name = re.sub(r'[<>:"/\\|?*]', "_", str(name))
    name = re.sub(r"\s+", " ", name).strip(" .")
    if len(name) > max_len:
        name = name[:max_len].rstrip(" .")
    return name or "untitled"


def _autodetect_canvas_dir() -> Path:
    env = os.getenv("DOWNLOAD_DIR")
    if env:
        return Path(env).expanduser()

    home = Path.home()
    cloud_storage = home / "Library" / "CloudStorage"
    if cloud_storage.exists():
        for folder in cloud_storage.iterdir():
            if folder.name.startswith("GoogleDrive"):
                candidate = folder / "My Drive" / "Canvas"
                if candidate.exists():
                    return candidate

    # Fallback to local folder used by canvas_sync.py when Google Drive isn't detected
    return Path(__file__).parent / "canvas_downloads"


def _is_external_url(url: str) -> bool:
    u = (url or "").strip().lower()
    if not u.startswith("http"):
        return False
    return CANVAS_HOST not in u


def _is_canvas_url(url: str) -> bool:
    u = (url or "").strip().lower()
    return u.startswith("http") and CANVAS_HOST in u


def _dedupe_preserve_order(items: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for it in items:
        it = (it or "").strip()
        if not it or it in seen:
            continue
        seen.add(it)
        out.append(it)
    return out


def _is_future_week(week_folder: Path) -> bool:
    """Check if a week folder represents a future week (hasn't started yet)."""
    try:
        week_json = week_folder / "week.json"
        if not week_json.exists():
            return True  # If no week.json, skip it
        data = _read_json(week_json)
        week_info = data.get("week") or {}
        start_date_str = week_info.get("start_date", "")
        if not start_date_str:
            return True  # If no start date, skip it
        start_date = date.fromisoformat(start_date_str.split("T")[0])  # Handle ISO datetime strings
        today = date.today()
        return start_date > today
    except Exception:
        # If parsing fails, assume it's future to be safe
        return True


def _week_folders(weekly_dir: Path, *, skip_future: bool = True) -> list[Path]:
    if not weekly_dir.exists():
        return []
    week_folders = [p for p in weekly_dir.iterdir() if p.is_dir() and (p / "week.json").exists()]
    if skip_future:
        week_folders = [f for f in week_folders if not _is_future_week(f)]
    return sorted(week_folders, key=lambda p: p.name)


def _pick_week_folder(weekly_dir: Path, week_key: str | None, *, skip_future: bool = True) -> Path:
    folders = _week_folders(weekly_dir, skip_future=skip_future)
    if not folders:
        raise FileNotFoundError(
            f"No week folders found in: {weekly_dir}"
            + (" (excluding future weeks)" if skip_future else "")
        )
    if not week_key or week_key.lower() in {"latest", "current"}:
        # Return the latest week that has started (not future)
        return folders[-1]
    for f in folders:
        if f.name.startswith(f"{week_key}_") or f.name == week_key:
            return f
    raise FileNotFoundError(f"Week '{week_key}' not found under: {weekly_dir}")


def _read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


@dataclass(frozen=True)
class PodcastPlan:
    title: str
    output_dir: Path
    output_basename: str
    urls: list[str]
    text: str


def _build_overall_text(week_payload: dict) -> str:
    wk = week_payload.get("week") or {}
    start = wk.get("start_date", "")
    end = wk.get("end_date", "")
    key = wk.get("key", "")

    items: list[dict] = week_payload.get("items") or []
    courses: dict[str, list[dict]] = {}
    for it in items:
        c = (it.get("course") or {}).get("name") or "Unknown course"
        courses.setdefault(c, []).append(it)

    lines: list[str] = []
    lines.append(f"Weekly overview: {key} ({start} to {end})")
    lines.append("")

    for course_name in sorted(courses.keys()):
        course_items = courses[course_name]
        lines.append(f"Course: {course_name}")
        # Focus on graded work
        graded = [i for i in course_items if i.get("kind") in {"assignment", "quiz"}]
        if graded:
            for g in graded:
                due = g.get("scheduled_at_local") or g.get("due_at") or ""
                lines.append(f"- {g.get('kind')}: {g.get('title')} {('(' + due + ')') if due else ''}".strip())
        else:
            lines.append("- No graded items detected in this week bundle.")
        lines.append("")

    lines.append("If you are commuting, prioritize graded items first, then review the prep resources.")
    return "\n".join(lines).strip() + "\n"


def _collect_course_sources(canvas_dir: Path, course_items: list[dict], *, max_sources: int) -> tuple[list[str], str]:
    """Return (urls, text) for Podcastfy.

    Podcastfy only extracts content from:
    - URLs (websites / YouTube)
    - local PDFs (file paths ending in .pdf)
    so we embed local .txt content into `text` instead of passing them as sources.
    """
    local_txt_files: list[Path] = []
    local_pdf_files: list[str] = []
    external_urls: list[str] = []

    graded = [i for i in course_items if i.get("kind") in {"assignment", "quiz"}]
    resources = [i for i in course_items if i.get("kind") == "resource"]

    for it in graded + resources:
        rel = it.get("local_relative_path")
        if rel:
            p = canvas_dir / rel
            if p.exists() and p.is_file():
                if p.suffix.lower() == ".pdf":
                    local_pdf_files.append(str(p))
                elif p.suffix.lower() == ".url":
                    # Windows shortcut format:
                    # [InternetShortcut]
                    # URL=https://...
                    try:
                        for line in p.read_text(encoding="utf-8", errors="ignore").splitlines():
                            if line.strip().lower().startswith("url="):
                                u = line.split("=", 1)[1].strip()
                                if _is_external_url(u):
                                    external_urls.append(u)
                                break
                    except Exception:
                        pass
                else:
                    # Most Canvas content is saved as readable .txt
                    local_txt_files.append(p)

        # Resource URLs (prefer external because Canvas usually needs auth)
        url = it.get("direct_url") or it.get("url") or ""
        if _is_external_url(url):
            external_urls.append(url)

    urls = _dedupe_preserve_order(local_pdf_files + external_urls)
    if max_sources and len(urls) > max_sources:
        urls = urls[:max_sources]

    # Build a small guiding text so the podcast is structured even if extraction fails on some sources.
    course_name = (course_items[0].get("course") or {}).get("name") if course_items else "Unknown course"
    wk = (course_items[0].get("week") if course_items else None) or ""
    lines: list[str] = []
    lines.append(f"Weekly study podcast for: {course_name} (week {wk})")
    lines.append("")
    if graded:
        lines.append("Graded items to complete this week:")
        for g in graded:
            due = g.get("scheduled_at_local") or g.get("due_at") or ""
            lines.append(f"- {g.get('kind')}: {g.get('title')} {('(' + due + ')') if due else ''}".strip())
    else:
        lines.append("No graded items were detected in this week bundle.")

    # Reading hints (from resource titles)
    reading_like = [r for r in resources if (r.get("resource_category") == "reading") or re.search(r"\b(read|reading|ch\.|chapter|pp\.)\b", (r.get("title") or ""), flags=re.IGNORECASE)]
    if reading_like:
        lines.append("")
        lines.append("Reading to focus on:")
        for r in reading_like[:12]:
            lines.append(f"- {r.get('title')}")

    lines.append("")
    lines.append("Now synthesize the key concepts from the provided sources, and end with a short checklist.")
    header = "\n".join(lines).strip() + "\n"

    # Append local text content (assignment/quiz instructions, module pages, etc.)
    # Keep this bounded so we don't explode context size.
    max_local_chars = 250_000
    chunks: list[str] = [header]
    used = len(header)

    for p in local_txt_files:
        try:
            body = p.read_text(encoding="utf-8", errors="ignore").strip()
        except Exception:
            continue
        if not body:
            continue

        block = f"\n\n--- SOURCE FILE: {p.name} ---\n{body}\n"
        if used + len(block) > max_local_chars:
            # Add a small note and stop appending
            chunks.append("\n\n(Additional local materials omitted for length.)\n")
            break
        chunks.append(block)
        used += len(block)

    return urls, "".join(chunks)


def _make_podcast_plans(
    canvas_dir: Path,
    week_folder: Path,
    week_payload: dict,
    *,
    per_class: bool,
    overall: bool,
    max_sources_per_class: int,
    overall_include_sources: bool,
    max_overall_sources: int,
) -> list[PodcastPlan]:
    items: list[dict] = week_payload.get("items") or []

    plans: list[PodcastPlan] = []

    if per_class:
        by_course: dict[str, list[dict]] = {}
        for it in items:
            cname = (it.get("course") or {}).get("name") or "Unknown course"
            by_course.setdefault(cname, []).append(it)

        out_root = week_folder / "podcasts" / "by_class"
        for cname in sorted(by_course.keys()):
            course_items = by_course[cname]
            urls, text = _collect_course_sources(
                canvas_dir, course_items, max_sources=max_sources_per_class
            )
            safe = _sanitize_filename(cname)
            output_dir = out_root / safe
            output_dir.mkdir(parents=True, exist_ok=True)
            output_basename = f"{_sanitize_filename(week_folder.name)}__{safe}.mp3"
            plans.append(
                PodcastPlan(
                    title=f"{week_folder.name} – {cname}",
                    output_dir=output_dir,
                    output_basename=output_basename,
                    urls=urls,
                    text=text,
                )
            )

    if overall:
        out_root = week_folder / "podcasts" / "overall"
        out_root.mkdir(parents=True, exist_ok=True)
        week_text = _build_overall_text(week_payload)

        urls: list[str] = []
        if overall_include_sources:
            # Keep this bounded; overall can explode.
            sources: list[str] = []
            for it in items:
                rel = it.get("local_relative_path")
                if rel:
                    p = canvas_dir / rel
                    if p.exists() and p.is_file():
                        sources.append(str(p))
                url = it.get("direct_url") or it.get("url") or ""
                if _is_external_url(url):
                    sources.append(url)
            urls = _dedupe_preserve_order(sources)
            if max_overall_sources and len(urls) > max_overall_sources:
                urls = urls[:max_overall_sources]

        output_basename = f"{_sanitize_filename(week_folder.name)}__OVERALL.mp3"
        plans.append(
            PodcastPlan(
                title=f"{week_folder.name} – Overall weekly overview",
                output_dir=out_root,
                output_basename=output_basename,
                urls=urls,
                text=week_text,
            )
        )

    return plans


def _podcastfy_generate(
    *,
    urls: list[str],
    text: str,
    output_dir: Path,
    tts_model: str,
    transcript_only: bool,
    longform: bool,
    llm_model_name: str,
    llm_api_key_label: str,
) -> Path:
    """
    Generate via Podcastfy, forcing outputs into output_dir via conversation_config.
    Returns final path (mp3 or transcript).
    """
    try:
        from podcastfy.client import generate_podcast
    except Exception as e:
        raise RuntimeError(
            "Podcastfy is not installed. Run: pip install -r requirements.txt"
        ) from e

    output_dir.mkdir(parents=True, exist_ok=True)
    transcripts_dir = output_dir / "transcripts"
    audio_dir = output_dir / "audio"
    transcripts_dir.mkdir(parents=True, exist_ok=True)
    audio_dir.mkdir(parents=True, exist_ok=True)

    conversation_config = {
        # Podcastfy reads output dirs from text_to_speech.output_directories in process_content()
        "text_to_speech": {
            "output_directories": {
                "transcripts": str(transcripts_dir),
                "audio": str(audio_dir),
            },
            "default_tts_model": tts_model,
        },
        # And generate_podcast() also checks top-level default_tts_model
        "default_tts_model": tts_model,
    }

    out = generate_podcast(
        urls=urls if urls else None,
        text=text if text else None,
        tts_model=tts_model,
        transcript_only=transcript_only,
        conversation_config=conversation_config,
        longform=longform,
        llm_model_name=llm_model_name,
        api_key_label=llm_api_key_label,
    )
    if not out:
        raise RuntimeError("Podcastfy returned no output path.")
    return Path(out)


def main() -> int:
    load_dotenv()
    _normalize_podcast_env()

    # Check API keys and provide helpful error messages
    try:
        llm_model_name, llm_api_key_label = _default_llm_config()
    except RuntimeError as e:
        print(f"❌ {e}")
        print("\n💡 Options:")
        print("   1. Get a free Gemini API key: https://aistudio.google.com/app/apikey")
        print("   2. Get an OpenAI API key: https://platform.openai.com/account/api-keys")
        print("   3. Skip podcasts: use --no-podcast flag")
        return 1

    ap = argparse.ArgumentParser(description="Generate weekly podcasts using Podcastfy.")
    ap.add_argument("--week", default="latest", help="Week key (e.g. 2026-W05) or 'latest'.")
    ap.add_argument("--all-weeks", action="store_true", help="Generate for all week folders.")
    ap.add_argument("--per-class", action="store_true", help="Generate one podcast per course for the week.")
    ap.add_argument("--overall", action="store_true", help="Generate an overall weekly overview podcast.")
    ap.add_argument("--overall-include-sources", action="store_true", help="Include sources in overall (can get large).")
    ap.add_argument("--max-sources-per-class", type=int, default=25, help="Cap sources passed to Podcastfy per class.")
    ap.add_argument("--max-overall-sources", type=int, default=40, help="Cap sources for overall (only if enabled).")
    ap.add_argument("--tts-model", default="edge", help="Podcastfy TTS model (edge=free, openai, elevenlabs, gemini). Default: edge (free, no API key needed).")
    ap.add_argument("--llm-model", default=None, help="Podcastfy transcript LLM model (defaults: gemini-2.5-flash or gpt-4o-mini).")
    ap.add_argument("--llm-api-key-label", default=None, help="Env var name for the transcript LLM API key (e.g. GEMINI_API_KEY, OPENAI_API_KEY).")
    ap.add_argument("--transcript-only", action="store_true", help="Generate transcript only (no audio).")
    ap.add_argument("--longform", action="store_true", help="Generate longform (Podcastfy longform=True).")
    ap.add_argument("--dry-run", action="store_true", help="Print planned inputs/outputs without generating.")
    args = ap.parse_args()

    if not args.per_class and not args.overall:
        # Default behavior: do both
        args.per_class = True
        args.overall = True

    # Use the already-validated config (or override if user specified)
    if args.llm_model:
        llm_model_name = args.llm_model
    if args.llm_api_key_label:
        llm_api_key_label = args.llm_api_key_label

    canvas_dir = _autodetect_canvas_dir()
    weekly_dir = canvas_dir / "_weekly"
    if not weekly_dir.exists():
        raise FileNotFoundError(f"Weekly dir not found: {weekly_dir} (run: python canvas_sync.py --bundle-weeks first)")

    print(f"📂 Looking for week folders in: {weekly_dir}")

    # Always skip future weeks - only process weeks that have started
    week_folders = _week_folders(weekly_dir, skip_future=True) if args.all_weeks else [_pick_week_folder(weekly_dir, args.week, skip_future=True)]
    
    if not week_folders:
        print(f"❌ No week folders found in: {weekly_dir}")
        print(f"   Run: python canvas_sync.py --bundle-weeks")
        return 1
    
    print(f"📅 Found {len(week_folders)} week folder(s) to process")
    
    # Count skipped future weeks for info message
    if args.all_weeks and weekly_dir.exists():
        all_folders = [p for p in weekly_dir.iterdir() if p.is_dir() and (p / "week.json").exists()]
        skipped_count = len(all_folders) - len(week_folders)
        if skipped_count > 0:
            print(f"⏭️  Skipping {skipped_count} future week(s) (details not available yet)")

    saved_podcasts: list[Path] = []
    failed_count = 0

    for week_folder in week_folders:
        print(f"\n📦 Processing week: {week_folder.name}")
        week_payload = _read_json(week_folder / "week.json")
        plans = _make_podcast_plans(
            canvas_dir,
            week_folder,
            week_payload,
            per_class=args.per_class,
            overall=args.overall,
            max_sources_per_class=args.max_sources_per_class,
            overall_include_sources=args.overall_include_sources,
            max_overall_sources=args.max_overall_sources,
        )

        if not plans:
            print(f"   ℹ️  No podcast plans generated for this week (no items or courses)")
            continue

        print(f"   📋 Generated {len(plans)} podcast plan(s)")
        print(f"   📂 Podcasts will be saved to: {week_folder / 'podcasts'}")

        for plan in plans:
            final_out = plan.output_dir / plan.output_basename
            print(f"   🎯 Target: {final_out}")
            if args.dry_run:
                print("=" * 80)
                print("PLAN:", plan.title)
                print("OUTPUT:", final_out)
                print("SOURCES:", len(plan.urls))
                for u in plan.urls[:10]:
                    print(" -", u)
                if len(plan.urls) > 10:
                    print(f" ... +{len(plan.urls) - 10} more")
                print("TEXT CHARS:", len(plan.text))
                continue

            print(f"📝 Generating: {plan.title}")
            print(f"   Output: {final_out}")
            try:
                generated = _podcastfy_generate(
                    urls=plan.urls,
                    text=plan.text,
                    output_dir=plan.output_dir,
                    tts_model=args.tts_model,
                    transcript_only=args.transcript_only,
                    longform=args.longform,
                    llm_model_name=llm_model_name,
                    llm_api_key_label=llm_api_key_label,
                )

                # Podcastfy may save to audio/ or transcripts/ subdirs, or directly to output_dir
                # Check common locations
                possible_locations = [
                    generated,  # What Podcastfy returned
                    plan.output_dir / "audio" / generated.name if generated.parent.name != "audio" else generated,
                    plan.output_dir / "transcripts" / generated.name if generated.parent.name != "transcripts" else generated,
                    plan.output_dir / generated.name,
                ]
                
                actual_file = None
                for loc in possible_locations:
                    if loc.exists() and loc.is_file():
                        actual_file = loc
                        break
                
                if not actual_file:
                    # If we can't find it, check what Podcastfy actually created
                    audio_dir = plan.output_dir / "audio"
                    transcripts_dir = plan.output_dir / "transcripts"
                    if audio_dir.exists():
                        audio_files = list(audio_dir.glob("*.mp3"))
                        if audio_files:
                            actual_file = audio_files[0]
                    if not actual_file and transcripts_dir.exists():
                        transcript_files = list(transcripts_dir.glob("*.txt"))
                        if transcript_files:
                            actual_file = transcript_files[0]
                
                if not actual_file:
                    raise FileNotFoundError(f"Podcastfy generated file not found. Checked: {possible_locations}")

                # Move/copy to our deterministic name in the week folder
                final_out.parent.mkdir(parents=True, exist_ok=True)
                if actual_file.resolve() != final_out.resolve():
                    try:
                        shutil.move(str(actual_file), str(final_out))
                    except Exception:
                        # If move fails (cross-device), fallback to copy+delete.
                        shutil.copy2(str(actual_file), str(final_out))
                        try:
                            actual_file.unlink(missing_ok=True)
                        except Exception:
                            pass

                print(f"✅ Saved: {final_out}")
                print(f"   📂 Location: {final_out.parent}")
                saved_podcasts.append(final_out)
            except Exception as e:
                failed_count += 1
                error_msg = str(e)
                if "AuthenticationError" in error_msg or "invalid_api_key" in error_msg or "401" in error_msg or "API key must be provided" in error_msg:
                    print(f"⚠️  Skipping podcast '{plan.title}': Missing/Invalid API key")
                    if "OpenAI" in error_msg or "openai" in error_msg.lower():
                        print(f"   TTS model '{args.tts_model}' requires OpenAI API key.")
                        print(f"   Use --tts-model edge (free) or set OPENAI_API_KEY in .env")
                    else:
                        print(f"   Fix your {llm_api_key_label} in .env or use --no-podcast to skip")
                else:
                    print(f"⚠️  Failed to generate podcast '{plan.title}': {error_msg[:200]}")
                # Continue with next podcast
                continue

    # Summary
    print("\n" + "=" * 80)
    print("📊 PODCAST GENERATION SUMMARY")
    print("=" * 80)
    if saved_podcasts:
        print(f"\n✅ Successfully generated {len(saved_podcasts)} podcast(s):")
        for p in saved_podcasts:
            print(f"   📻 {p}")
            print(f"      Location: {p.parent}")
    else:
        print("\n❌ No podcasts were successfully generated")
    if failed_count > 0:
        print(f"\n⚠️  {failed_count} podcast(s) failed (see errors above)")
    if not saved_podcasts and failed_count == 0:
        print("\nℹ️  No podcasts to generate (no week folders with content found)")
    print("\n" + "=" * 80)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())

