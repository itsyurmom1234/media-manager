#!/usr/bin/env python3
"""
reformat_for_jellyfin.py

Converts a "Videos" library laid out as:

    /library-root/
        Playlist Name A/
            01Some Title.mp4
            01Some Title.nfo
            02 "Another Title".mp4
            02 "Another Title".nfo
        Playlist Name B/            <- no numbering, order doesn't matter
            Random Clip.mp4
            Random Clip.nfo
            Another One.mp4

into a Jellyfin-friendly TV Shows structure:

    /library-root/
        Playlist Name A/
            Season 01/
                Playlist Name A S01E01 - Some Title.mp4
                Playlist Name A S01E01 - Some Title.nfo
                Playlist Name A S01E02 - Another Title.mp4
                Playlist Name A S01E02 - Another Title.nfo
        Playlist Name B/
            Season 01/
                Playlist Name B S01E01 - Random Clip.mp4
                Playlist Name B S01E01 - Random Clip.nfo
                Playlist Name B S01E02 - Another One.mp4

Usage:
    python reformat_for_jellyfin.py "D:\Videos"            # dry run (default) - prints planned moves, changes nothing
    python reformat_for_jellyfin.py "D:\Videos" --apply     # actually performs the moves

Notes:
- Handles two numbered filename styles seen in your library:
    "01Title.ext"            (digits run straight into the title)
    "01 "Title".ext"         (digits, space, title wrapped in literal quotes)
- If a playlist folder has NO parseable episode numbers on any file, files are
  sorted alphabetically and numbered sequentially 1..N (order doesn't matter
  for that folder per your note).
- If a folder has a MIX of numbered and unnumbered files, numbered files keep
  their original playlist number as the episode number; unnumbered files are
  appended afterward in alphabetical order, continuing the numbering.
- .nfo files (and any other sidecar file sharing the same base filename, e.g.
  -thumb.jpg) are moved/renamed alongside their video automatically.
- Already-processed playlists (ones that already contain a "Season 01" folder)
  are skipped, so it's safe to re-run.
"""

import argparse
import re
import sys
from pathlib import Path

VIDEO_EXTS = {".mp4", ".mkv", ".webm", ".avi", ".mov", ".m4v", ".flv", ".wmv"}

# Characters not safe in Windows/Linux filenames
ILLEGAL_CHARS = r'<>:"/\\|?*'


def sanitize(title: str) -> str:
    for ch in ILLEGAL_CHARS:
        title = title.replace(ch, "")
    return title.strip(" .")


def parse_numbered(stem: str):
    """Try to pull (episode_number, title) out of a filename stem.
    Returns (None, stem) if no leading number pattern is found."""
    # Style: 01 "Title"   (digits, optional space, quoted title)
    m = re.match(r'^(\d+)\s*"(.+)"$', stem)
    if m:
        return int(m.group(1)), m.group(2).strip()

    # Style: 01Title  or  01 - Title  or  01_Title  (digits run into title,
    # possibly with a separator)
    m = re.match(r'^(\d+)[\s\-_.]*(.+)$', stem)
    if m and m.group(2).strip():
        return int(m.group(1)), m.group(2).strip()

    return None, stem.strip()


def group_sidecars(files, video_stem):
    """Return all files in `files` that share the video's base stem
    (nfo, thumbnails, subtitles, etc.)."""
    return [f for f in files if f.stem == video_stem]


def plan_playlist(folder: Path):
    """Build a list of (old_path, new_path) moves for one playlist folder."""
    all_files = [f for f in folder.iterdir() if f.is_file()]
    videos = [f for f in all_files if f.suffix.lower() in VIDEO_EXTS]
    if not videos:
        return []

    numbered = []    # (ep_num, title, path)
    unnumbered = []  # (title, path)

    for v in videos:
        ep, title = parse_numbered(v.stem)
        if ep is not None:
            numbered.append((ep, title, v))
        else:
            unnumbered.append((title, v))

    numbered.sort(key=lambda x: x[0])
    unnumbered.sort(key=lambda x: x[0].lower())

    moves = []
    season_dir = folder / "Season 01"
    playlist_name = folder.name

    used_eps = set()

    def next_free_ep(start):
        n = start
        while n in used_eps:
            n += 1
        return n

    # Numbered files keep their original number (bumped forward only if a
    # collision occurs, which would only happen with duplicate numbers)
    for ep, title, path in numbered:
        ep = next_free_ep(ep)
        used_eps.add(ep)
        new_title = sanitize(title)
        new_stem = f"{playlist_name} S01E{ep:02d} - {new_title}"
        for sidecar in group_sidecars(all_files, path.stem):
            moves.append((sidecar, season_dir / f"{new_stem}{sidecar.suffix}"))

    # Unnumbered files get the next free episode numbers, in alpha order
    next_ep = 1
    for title, path in unnumbered:
        next_ep = next_free_ep(next_ep)
        used_eps.add(next_ep)
        new_title = sanitize(title)
        new_stem = f"{playlist_name} S01E{next_ep:02d} - {new_title}"
        for sidecar in group_sidecars(all_files, path.stem):
            moves.append((sidecar, season_dir / f"{new_stem}{sidecar.suffix}"))
        next_ep += 1

    return moves


def main():
    parser = argparse.ArgumentParser(description="Reformat a YouTube playlist library into Jellyfin TV Show layout.")
    parser.add_argument("library_root", help="Path to the folder containing your playlist subfolders")
    parser.add_argument("--apply", action="store_true", help="Actually perform the moves (default is dry run)")
    args = parser.parse_args()

    root = Path(args.library_root)
    if not root.is_dir():
        print(f"Not a directory: {root}")
        sys.exit(1)

    total_moves = 0
    for playlist_folder in sorted(p for p in root.iterdir() if p.is_dir()):
        if (playlist_folder / "Season 01").exists():
            print(f"[skip] {playlist_folder.name} (already has Season 01)")
            continue

        moves = plan_playlist(playlist_folder)
        if not moves:
            print(f"[skip] {playlist_folder.name} (no video files found)")
            continue

        print(f"\n[{playlist_folder.name}]")
        for old, new in moves:
            print(f"  {old.name}  ->  Season 01/{new.name}")
            total_moves += 1
            if args.apply:
                new.parent.mkdir(parents=True, exist_ok=True)
                old.rename(new)

    print(f"\n{total_moves} file(s) {'moved' if args.apply else 'would be moved'}.")
    if not args.apply:
        print("Dry run only — nothing was changed. Re-run with --apply to perform the moves.")


if __name__ == "__main__":
    main()
