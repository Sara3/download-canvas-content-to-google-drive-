#!/usr/bin/env python3
"""
Canvas Course Downloader to Google Drive
Downloads course content one course at a time for verification.

Usage:
    python download_to_gdrive.py
"""

import os
import sys
import json
import asyncio
from pathlib import Path
from datetime import datetime

# Import the main downloader
from canvas_api_downloader import CanvasAPIDownloader, CANVAS_URL, SESSION_FILE

# Google Drive path
GDRIVE_PATH = Path.home() / "Library/CloudStorage/GoogleDrive-pubandsubs@gmail.com/My Drive/Canvas"


class InteractiveCourseDownloader:
    def __init__(self):
        self.downloader = CanvasAPIDownloader(force_full_sync=True)
        self.courses = []
    
    async def initialize(self) -> bool:
        """Load session and fetch courses."""
        if not self.downloader.load_session():
            print("\n❌ No valid session found!")
            print("   Run: HEADLESS=false python canvas_downloader.py")
            return False
        
        print("\n📚 Fetching your enrolled courses...")
        self.courses = await self.downloader.get_courses()
        
        if not self.courses:
            print("❌ No courses found!")
            return False
        
        return True
    
    def display_courses(self):
        """Show all courses with their download status."""
        print("\n" + "=" * 70)
        print("YOUR ENROLLED COURSES")
        print("=" * 70)
        
        for i, course in enumerate(self.courses, 1):
            name = course.get('name', 'Unknown Course')
            course_id = course.get('id')
            
            # Check if already downloaded
            safe_name = self.downloader.sanitize_filename(name)
            course_dir = GDRIVE_PATH / safe_name
            status = "✅ Downloaded" if course_dir.exists() else "⬜ Not downloaded"
            
            # Count files if downloaded
            file_count = ""
            if course_dir.exists():
                files = list(course_dir.rglob("*"))
                file_count = f" ({len([f for f in files if f.is_file()])} files)"
            
            print(f"\n  [{i}] {name}")
            print(f"      Status: {status}{file_count}")
            print(f"      Canvas ID: {course_id}")
        
        print("\n" + "-" * 70)
        print("  [A] Download ALL courses")
        print("  [R] Re-download ALL courses (fresh start)")
        print("  [Q] Quit")
        print("-" * 70)
    
    async def download_course(self, course: dict, course_num: int, total: int):
        """Download a single course to Google Drive."""
        name = course.get('name', 'Unknown')
        course_id = course.get('id')
        safe_name = self.downloader.sanitize_filename(name)
        course_dir = GDRIVE_PATH / safe_name
        
        print(f"\n{'='*70}")
        print(f"DOWNLOADING COURSE {course_num}/{total}")
        print(f"{'='*70}")
        print(f"📖 {name}")
        print(f"📂 Saving to: {course_dir}")
        print(f"{'='*70}\n")
        
        # Temporarily override the download dir
        import canvas_api_downloader
        original_dir = canvas_api_downloader.DOWNLOAD_DIR
        canvas_api_downloader.DOWNLOAD_DIR = GDRIVE_PATH
        
        # Create a fresh downloader for this course
        self.downloader = CanvasAPIDownloader(force_full_sync=True)
        self.downloader.load_session()
        
        try:
            await self.downloader.download_course(course)
            print(f"\n✅ Course download complete!")
            print(f"📂 Files saved to: {course_dir}")
            
            # Show what was downloaded
            if course_dir.exists():
                print(f"\n📊 Downloaded content:")
                for subdir in sorted(course_dir.iterdir()):
                    if subdir.is_dir():
                        files = list(subdir.rglob("*"))
                        file_count = len([f for f in files if f.is_file()])
                        print(f"   📁 {subdir.name}: {file_count} files")
                    elif subdir.is_file() and not subdir.name.startswith('_'):
                        print(f"   📄 {subdir.name}")
            
            return True
        except Exception as e:
            print(f"\n❌ Error downloading course: {e}")
            return False
        finally:
            canvas_api_downloader.DOWNLOAD_DIR = original_dir
    
    async def run_interactive(self):
        """Main interactive loop."""
        print("""
╔═══════════════════════════════════════════════════════════════════╗
║        Canvas Course Downloader to Google Drive                   ║
╚═══════════════════════════════════════════════════════════════════╝
        """)
        
        # Ensure Google Drive folder exists
        GDRIVE_PATH.mkdir(parents=True, exist_ok=True)
        print(f"📂 Google Drive folder: {GDRIVE_PATH}")
        
        if not await self.initialize():
            return
        
        while True:
            self.display_courses()
            
            choice = input("\nEnter your choice: ").strip().upper()
            
            if choice == 'Q':
                print("\n👋 Goodbye!")
                break
            
            elif choice == 'A':
                # Download all courses that haven't been downloaded
                print("\n📥 Downloading all courses...")
                for i, course in enumerate(self.courses, 1):
                    safe_name = self.downloader.sanitize_filename(course.get('name', ''))
                    course_dir = GDRIVE_PATH / safe_name
                    if course_dir.exists():
                        print(f"\n⏭️  Skipping {course.get('name')} (already downloaded)")
                        continue
                    
                    await self.download_course(course, i, len(self.courses))
                    
                    if i < len(self.courses):
                        cont = input("\n➡️  Continue to next course? [Y/n]: ").strip().lower()
                        if cont == 'n':
                            print("Stopping. You can resume later.")
                            break
            
            elif choice == 'R':
                # Re-download everything
                confirm = input("\n⚠️  This will re-download ALL courses. Continue? [y/N]: ").strip().lower()
                if confirm == 'y':
                    for i, course in enumerate(self.courses, 1):
                        await self.download_course(course, i, len(self.courses))
                        
                        if i < len(self.courses):
                            cont = input("\n➡️  Continue to next course? [Y/n]: ").strip().lower()
                            if cont == 'n':
                                print("Stopping. You can resume later.")
                                break
            
            elif choice.isdigit():
                idx = int(choice) - 1
                if 0 <= idx < len(self.courses):
                    course = self.courses[idx]
                    await self.download_course(course, idx + 1, len(self.courses))
                    
                    input("\n⏎ Press Enter to continue...")
                else:
                    print(f"\n❌ Invalid choice. Enter 1-{len(self.courses)}")
            
            else:
                print("\n❌ Invalid choice. Try again.")


async def main():
    downloader = InteractiveCourseDownloader()
    await downloader.run_interactive()


if __name__ == "__main__":
    asyncio.run(main())
