#!/usr/bin/env python3
"""
make_posters.py

Grabs frames from your YouTube-playlist episodes and saves them as local images so
Jellyfin shows something recognizable in the library without needing internet
metadata fetchers.

Jellyfin looks for these automatically (local files always win over fetchers):
    <Playlist Folder>/poster.jpg                      <- series poster (frame from first episode)
    <Playlist Folder>/Season 01/poster.jpg            <- season poster (same frame)
    <Playlist Folder>/Season 01/<episode>-thumb.jpg   <- one thumbnail per episode

Requires ffmpeg and ffprobe on PATH.

Usage:
    python3 make_posters.py "/mnt/user/LinuxIsos/yt-shows"                        # dry run
    python3 make_posters.py "/mnt/user/LinuxIsos/yt-shows" --apply                # create missing images
    python3 make_posters.py "/mnt/user/LinuxIsos/yt-shows" --apply --force        # overwrite existing images too
    python3 make_posters.py "/mnt/user/LinuxIsos/yt-shows" --apply --no-episodes  # posters only (old behaviour)
"""

import argparse
import re
import subprocess
import sys
from pathlib import Path

VIDEO_EXTS = {".mp4", ".mkv", ".webm", ".avi", ".mov", ".m4v", ".flv", ".wmv"}
EP_RE = re.compile(r"S01E(\d+)", re.IGNORECASE)


def get_duration(video_path: Path):
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "csv=p=0", str(video_path)],
            capture_output=True, text=True, timeout=30
        )
        return float(out.stdout.strip())
    except Exception:
        return None


def extract_frame(video_path: Path, out_path: Path) -> bool:
    duration = get_duration(video_path)
    if duration:
        timestamp = max(2, min(duration * 0.1, 30))
        if timestamp >= duration:  # very short clip: just take the first frame
            timestamp = 0
    else:
        timestamp = 5

    # Try the chosen timestamp first, then fall back to the very first frame.
    for ts in dict.fromkeys((timestamp, 0)):
        try:
            subprocess.run(
                ["ffmpeg", "-y", "-ss", str(ts), "-i", str(video_path),
                 "-frames:v", "1", "-q:v", "3", str(out_path)],
                capture_output=True, text=True, timeout=60
            )
        except subprocess.TimeoutExpired:
            pass
        if out_path.exists() and out_path.stat().st_size > 0:
            return True
        # Don't leave an empty file behind: it would be treated as "already exists" next run.
        if out_path.exists():
            out_path.unlink()
    return False


def list_episodes(season_folder: Path):
    return sorted(
        f for f in season_folder.iterdir()
        if f.is_file() and f.suffix.lower() in VIDEO_EXTS
    )


def first_episode(season_folder: Path):
    candidates = []
    for f in list_episodes(season_folder):
        m = EP_RE.search(f.stem)
        ep_num = int(m.group(1)) if m else 999999
        candidates.append((ep_num, f))
    if not candidates:
        return None
    candidates.sort(key=lambda x: x[0])
    return candidates[0][1]


def has_image(path: Path) -> bool:
    # An empty file means an earlier extraction failed, so treat it as missing.
    return path.exists() and path.stat().st_size > 0


def episode_thumb(video: Path) -> Path:
    # Jellyfin reads "<episode filename without extension>-thumb.jpg" as that episode's image.
    return video.with_name(video.stem + "-thumb.jpg")


def main():
    parser = argparse.ArgumentParser(
        description="Extract poster and per-episode thumbnail images from your playlist videos.")
    parser.add_argument("library_root", help="Path to the folder containing your playlist subfolders")
    parser.add_argument("--apply", action="store_true", help="Actually create the images (default is dry run)")
    parser.add_argument("--force", action="store_true", help="Overwrite existing poster/thumbnail images")
    parser.add_argument("--no-episodes", action="store_true", help="Only make posters, skip per-episode thumbnails")
    args = parser.parse_args()

    root = Path(args.library_root)
    if not root.is_dir():
        print(f"Not a directory: {root}")
        sys.exit(1)

    created = 0
    failed = 0

    for playlist_folder in sorted(p for p in root.iterdir() if p.is_dir()):
        season_folder = playlist_folder / "Season 01"
        if not season_folder.is_dir():
            print(f"[skip] {playlist_folder.name} (no Season 01 folder)")
            continue

        video = first_episode(season_folder)
        if not video:
            print(f"[skip] {playlist_folder.name} (no video files found)")
            continue

        jobs = []  # (source video, image to create)

        for target in (playlist_folder / "poster.jpg", season_folder / "poster.jpg"):
            if has_image(target) and not args.force:
                print(f"[skip] {target} already exists (use --force to overwrite)")
            else:
                jobs.append((video, target))
        poster_jobs = len(jobs)

        if not args.no_episodes:
            for ep in list_episodes(season_folder):
                thumb = episode_thumb(ep)
                if has_image(thumb) and not args.force:
                    continue
                jobs.append((ep, thumb))

        if not jobs:
            continue

        print(f"[{playlist_folder.name}] {poster_jobs} poster(s), "
              f"{len(jobs) - poster_jobs} episode thumbnail(s) to create")

        if args.apply:
            for src, target in jobs:
                if extract_frame(src, target):
                    created += 1
                else:
                    failed += 1
                    print(f"     WARNING: extraction failed for {target}")

    if args.apply:
        print(f"\nDone: {created} image(s) created, {failed} failed.")
    else:
        print("\nDry run only — nothing was created. Re-run with --apply to generate the images.")


if __name__ == "__main__":
    main()