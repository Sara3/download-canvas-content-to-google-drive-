#!/usr/bin/env python3
"""
Zoom LTI (Advantage) recordings sync

Purpose
-------
Many Canvas courses expose Zoom via LTI (e.g. TechConnect Zoom). Canvas content sync
cannot download external-tool content, but we *can* automate the Zoom LTI portal to
download Cloud Recordings (especially "Audio only") for offline listening.

How it works
------------
- Reuses the Playwright storage state from `login_refresh.py` (stored in .canvas_session.json)
- Optionally visits the Canvas "Zoom external tool" page to establish LTI session
- Navigates to https://applications.zoom.us/lti/advantage
- Clicks "Cloud Recordings", opens each recording, downloads "Audio only" when available
- Tracks downloads in a small local state file to avoid re-downloading

Notes
-----
- This is best-effort UI automation. Zoom may change selectors.
- Your institution may require you to launch Zoom via Canvas at least once to seed cookies.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import subprocess
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from dotenv import load_dotenv
from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeoutError


CANVAS_URL = "https://canvas.santarosa.edu"
DEFAULT_ZOOM_LTI_ADVANTAGE_URL = "https://applications.zoom.us/lti/advantage"
SESSION_FILE = Path(__file__).parent / ".canvas_session.json"


def _sanitize_filename(name: str, max_len: int = 140) -> str:
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

    local = Path(__file__).parent / "canvas_downloads"
    local.mkdir(parents=True, exist_ok=True)
    return local


def _pick_course_dir(canvas_dir: Path, course_filter: str) -> Path:
    course_filter_l = (course_filter or "").strip().lower()
    if not course_filter_l:
        raise ValueError("--course is required (e.g. 'KIN84')")

    candidates: list[Path] = []
    for p in canvas_dir.iterdir():
        if not p.is_dir():
            continue
        if p.name.startswith("_"):
            continue
        if course_filter_l in p.name.lower():
            candidates.append(p)

    if not candidates:
        raise FileNotFoundError(
            f"No course folder matching '{course_filter}' under: {canvas_dir}\n"
            f"Run `python canvas_sync.py --course \"{course_filter}\"` first, or set DOWNLOAD_DIR."
        )
    # Prefer the shortest match (usually the actual course folder).
    return sorted(candidates, key=lambda x: len(x.name))[0]


def _load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _read_zoom_links(course_dir: Path) -> list[dict[str, Any]]:
    p = course_dir / "_zoom_links.json"
    if not p.exists():
        return []
    try:
        payload = _load_json(p)
        links = payload.get("links") or []
        return [l for l in links if isinstance(l, dict)]
    except Exception:
        return []


def _default_canvas_zoom_tool_url(zoom_links: list[dict[str, Any]]) -> Optional[str]:
    """
    Best-effort find a Canvas external tool link.
    In practice you may need to set CANVAS_ZOOM_TOOL_URL, e.g.:
      https://canvas.santarosa.edu/courses/83136/external_tools/34904
    """
    for entry in zoom_links:
        u = (entry.get("url") or "").strip()
        if not u:
            continue
        if "canvas.santarosa.edu" in u and "/external_tools/" in u:
            return u
    return None


@dataclass(frozen=True)
class RecordingLink:
    href: str
    label: str


def _state_paths(out_dir: Path) -> tuple[Path, Path]:
    state_dir = out_dir / "_state"
    state_dir.mkdir(parents=True, exist_ok=True)
    return state_dir / "zoom_lti_sync_state.json", state_dir / "zoom_lti_sync_last_run.json"


def _load_state(state_path: Path) -> dict[str, Any]:
    if not state_path.exists():
        return {"downloaded": {}, "created_at": datetime.now().isoformat()}
    try:
        return json.loads(state_path.read_text(encoding="utf-8"))
    except Exception:
        return {"downloaded": {}, "created_at": datetime.now().isoformat()}


def _save_state(state_path: Path, state: dict[str, Any]) -> None:
    state_path.write_text(json.dumps(state, indent=2, ensure_ascii=False), encoding="utf-8")


async def _maybe_click_redirect_here(page) -> None:
    # Canvas Zoom external tool sometimes shows "Redirect to Zoom... please click here."
    try:
        link = page.locator("a:has-text('click here')").first
        if await link.count():
            # Might open in same page or a popup.
            try:
                async with page.expect_popup(timeout=2500) as pop:
                    await link.click()
                popup = await pop.value
                await popup.wait_for_load_state("domcontentloaded")
                await popup.close()
            except PlaywrightTimeoutError:
                await link.click()
                await page.wait_for_load_state("domcontentloaded")
    except Exception:
        return


async def _goto_zoom_advantage(page, url: str) -> None:
    await page.goto(url, wait_until="domcontentloaded")
    # Allow SPA rendering.
    await page.wait_for_timeout(1500)


async def _click_tab(page, tab_name: str) -> None:
    # Try tab role first, then fall back to text search.
    try:
        tab = page.get_by_role("tab", name=tab_name)
        if await tab.count():
            await tab.first.click()
            await page.wait_for_timeout(1000)
            return
    except Exception:
        pass

    loc = page.locator(f"text={tab_name}").first
    if await loc.count():
        await loc.click()
        await page.wait_for_timeout(1000)


async def _extract_recording_links_from_page(page) -> list[RecordingLink]:
    """
    Zoom LTI pages change often. We attempt multiple heuristics:
    - anchors containing 'recording/detail'
    - anchors containing '/recording' under /lti/
    """
    # Try JS extraction for speed and fewer roundtrips.
    candidates: list[dict[str, str]] = []
    for selector in [
        'a[href*="recording/detail"]',
        'a[href*="/recording/"]',
        'a[href*="/lti/rich/home/recording"]',
    ]:
        try:
            rows = await page.eval_on_selector_all(
                selector,
                "els => els.map(e => ({href: e.href, text: (e.innerText || '').trim()}))",
            )
            if rows:
                candidates.extend([r for r in rows if isinstance(r, dict)])
        except Exception:
            continue

    # Dedupe + filter
    seen: set[str] = set()
    out: list[RecordingLink] = []
    for c in candidates:
        href = (c.get("href") or "").strip()
        if not href or href in seen:
            continue
        seen.add(href)
        label = (c.get("text") or "").strip()
        if not label:
            label = href
        out.append(RecordingLink(href=href, label=label))

    return out


async def _open_recording_detail(page, href: str) -> None:
    await page.goto(href, wait_until="domcontentloaded")
    await page.wait_for_timeout(1200)


async def _download_audio_only_from_detail(page, out_dir: Path, *, dry_run: bool) -> Optional[Path]:
    """
    Attempt to download "Audio only" asset from the recording detail page.
    Returns local path if downloaded, else None.
    """
    # Best-effort title/date parsing from page content.
    title_text = ""
    try:
        # The H1 in the screenshot contains the session title.
        h1 = page.locator("h1").first
        if await h1.count():
            title_text = (await h1.inner_text()).strip()
    except Exception:
        pass

    # Prefer the "Audio only" tile (screenshot shows "Audio only-1 (73 MB)")
    audio_tile = page.locator("text=Audio only").first
    if not await audio_tile.count():
        # Some tenants label it "Audio" or "Audio Only"
        audio_tile = page.locator("text=Audio").first
        if not await audio_tile.count():
            return None

    if dry_run:
        return out_dir / "_DRY_RUN_"

    # Sometimes clicking the tile opens a view where a "Download" button appears.
    await audio_tile.click()
    await page.wait_for_timeout(1200)

    # Find a download control.
    download_candidate = None
    for sel in [
        "a:has-text('Download')",
        "button:has-text('Download')",
        "text=Download",
    ]:
        loc = page.locator(sel).first
        try:
            if await loc.count():
                download_candidate = loc
                break
        except Exception:
            continue

    if download_candidate is None:
        # In some variants the download is available directly on the tile without opening.
        return None

    # Trigger download and save.
    try:
        async with page.expect_download(timeout=60_000) as dl_info:
            await download_candidate.click()
        download = await dl_info.value
    except PlaywrightTimeoutError:
        return None

    suggested = _sanitize_filename(download.suggested_filename)
    # Prefix with session title if available.
    prefix = _sanitize_filename(title_text) if title_text else "zoom_recording"
    final_name = f"{prefix}__{suggested}"
    final_path = out_dir / final_name
    await download.save_as(str(final_path))
    return final_path


def _ffmpeg_convert_to_mp3(src: Path, mp3_path: Path) -> None:
    mp3_path.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        "ffmpeg",
        "-y",
        "-i",
        str(src),
        "-vn",
        "-acodec",
        "libmp3lame",
        "-q:a",
        "2",
        str(mp3_path),
    ]
    subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


async def main_async() -> int:
    load_dotenv()

    ap = argparse.ArgumentParser(description="Download Zoom LTI (Advantage) Cloud Recordings for a Canvas course.")
    ap.add_argument("--course", default="KIN84", help="Course folder name filter under DOWNLOAD_DIR (default: KIN84).")
    ap.add_argument("--canvas-launch-url", default=os.getenv("CANVAS_ZOOM_TOOL_URL"), help="Canvas Zoom external tool URL (recommended).")
    ap.add_argument("--zoom-advantage-url", default=os.getenv("ZOOM_LTI_ADVANTAGE_URL", DEFAULT_ZOOM_LTI_ADVANTAGE_URL))
    ap.add_argument("--headless", action="store_true", help="Run browser headless (default: headed).")
    ap.add_argument("--limit", type=int, default=30, help="Max new recordings to attempt per run.")
    ap.add_argument("--dry-run", action="store_true", help="List recordings that would be downloaded, but do nothing.")
    ap.add_argument("--convert-mp3", action="store_true", help="Convert downloaded audio/video to mp3 (requires ffmpeg).")
    args = ap.parse_args()

    if not SESSION_FILE.exists():
        raise FileNotFoundError(
            f"Session file not found: {SESSION_FILE}\n"
            "Run `python login_refresh.py` first to create it."
        )

    canvas_dir = _autodetect_canvas_dir()
    course_dir = _pick_course_dir(canvas_dir, args.course)
    out_dir = course_dir / "zoom_recordings"
    out_dir.mkdir(parents=True, exist_ok=True)
    state_path, last_run_path = _state_paths(out_dir)
    state = _load_state(state_path)
    downloaded: dict[str, Any] = state.get("downloaded") or {}

    # If the user didn't provide a Canvas launch URL, try to infer one.
    canvas_launch_url = (args.canvas_launch_url or "").strip() or None
    if not canvas_launch_url:
        inferred = _default_canvas_zoom_tool_url(_read_zoom_links(course_dir))
        canvas_launch_url = inferred

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=bool(args.headless),
            args=["--disable-blink-features=AutomationControlled"],
        )
        context = await browser.new_context(
            user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
            viewport={"width": 1280, "height": 720},
            storage_state=str(SESSION_FILE),
            accept_downloads=True,
        )
        page = await context.new_page()
        page.set_default_timeout(60_000)

        # Step 1: optionally launch Zoom from Canvas to establish the LTI session.
        if canvas_launch_url:
            await page.goto(canvas_launch_url, wait_until="domcontentloaded")
            await page.wait_for_timeout(1500)
            await _maybe_click_redirect_here(page)
            # Some tenants redirect automatically; give it a moment.
            await page.wait_for_timeout(1500)

        # Step 2: open the Zoom LTI portal.
        await _goto_zoom_advantage(page, args.zoom_advantage_url)

        # Step 3: open Cloud Recordings list.
        await _click_tab(page, "Cloud Recordings")

        # Step 4: extract recording links.
        links = await _extract_recording_links_from_page(page)

        # Persist a small run snapshot for debugging.
        last_run_path.write_text(
            json.dumps(
                {
                    "ran_at": datetime.now().isoformat(),
                    "course_dir": str(course_dir),
                    "zoom_advantage_url": args.zoom_advantage_url,
                    "canvas_launch_url": canvas_launch_url,
                    "found_links": [{"href": l.href, "label": l.label} for l in links[:200]],
                },
                indent=2,
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )

        if not links:
            # Save a screenshot for troubleshooting.
            try:
                await page.screenshot(path=str(out_dir / "zoom_lti_no_links.png"), full_page=True)
            except Exception:
                pass
            await context.close()
            await browser.close()
            raise RuntimeError(
                "Could not find any recording links on the Zoom LTI portal.\n"
                "Try running headed (no --headless) and ensure you can see Cloud Recordings in the opened browser.\n"
                "If your institution requires launching from Canvas, set CANVAS_ZOOM_TOOL_URL in .env."
            )

        # Step 5: download new recordings.
        new_count = 0
        for rec in links:
            if new_count >= max(0, int(args.limit)):
                break

            key = rec.href
            if key in downloaded and Path(downloaded[key].get("path", "")).exists():
                continue

            if args.dry_run:
                print("[DRY RUN] Would download:", rec.label, rec.href)
                new_count += 1
                continue

            await _open_recording_detail(page, rec.href)
            downloaded_path = await _download_audio_only_from_detail(page, out_dir, dry_run=False)
            if not downloaded_path:
                # Fallback: snapshot the page and keep going.
                try:
                    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
                    await page.screenshot(path=str(out_dir / f"zoom_lti_download_failed_{ts}.png"), full_page=True)
                except Exception:
                    pass
                continue

            downloaded[key] = {
                "href": rec.href,
                "label": rec.label,
                "path": str(downloaded_path),
                "downloaded_at": datetime.now().isoformat(),
            }
            _save_state(state_path, {"downloaded": downloaded, "updated_at": datetime.now().isoformat()})
            new_count += 1

            if args.convert_mp3:
                try:
                    mp3_name = f"{downloaded_path.stem}.mp3"
                    mp3_path = downloaded_path.with_name(mp3_name)
                    _ffmpeg_convert_to_mp3(downloaded_path, mp3_path)
                except Exception:
                    # Conversion is optional; keep the original.
                    pass

        await context.close()
        await browser.close()

    # Save final state
    _save_state(state_path, {"downloaded": downloaded, "updated_at": datetime.now().isoformat()})
    print(f"✅ Zoom sync done. Saved to: {out_dir}")
    return 0


def main() -> int:
    try:
        return asyncio.run(main_async())
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())

