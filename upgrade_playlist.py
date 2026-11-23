#!/usr/bin/env python3
"""Upgrade low-resolution videos in place, keeping their names, .nfo files,
posters and Jellyfin identity.

Runs inside the download-worker container (it has yt-dlp, deno, ffprobe and
your /shows and /downloads mounts). Default is a DRY RUN that only shows what
would happen. Add --apply to actually download and replace files.

Playlist show:
  python /upgrade_playlist.py --show "Camp Rock" --url "<playlist url>" [--apply]

Single video (a file in your regular-videos folder):
  python /upgrade_playlist.py --file "Server Room Update" --url "<video url>" [--apply]
  python /upgrade_playlist.py --file "Server Room Update" --search [--apply]   (finds the video itself)

--search looks the title up on YouTube and only accepts a result whose title is
very close AND whose length is within 5% of your file. Run it without --apply
first to see which video it picked.

--max-height N raises/lowers the resolution cap (default 1080). Note this is a
HEIGHT cap: a 2:1 video's "1080p" tier is 1920x960, and its 4K tier is 3840x1920.

An old file is only replaced when ALL of these hold:
  - YouTube offers a higher resolution than the current file
  - the new download is higher resolution than the current file
  - the new duration is within 5% (or 10 s) of the current duration
Otherwise the old file is left untouched and the reason is logged.
"""
import argparse
import json
import os
import random
import re
import shutil
import subprocess
import sys
import time
import unicodedata
from difflib import SequenceMatcher

SHOWS_DIR = os.environ.get("SHOWS_DIR", "/shows")
DOWNLOAD_DIR = os.environ.get("DOWNLOAD_DIR", "/downloads")
STAGING_ROOT = "/tmp/upgrade-staging"

# Default matches the format string in app/tasks.py; --max-height overrides it
MAX_HEIGHT = 1080


def format_string():
    h = MAX_HEIGHT
    return f"bv*[height<={h}]+ba/b[height<={h}]/bv*+ba/b"

RETRYABLE = ("403", "forbidden", "429", "too many requests", "503", "service unavailable")
# Errors that mean the video itself is gone/blocked (not throttling) - skip, don't count as failures
PERMANENT = ("not available", "unavailable", "private video", "has been removed", "removed by",
             "deleted", "terminated", "copyright", "members-only", "join this channel",
             "blocked it", "confirm your age")
EP_RE = re.compile(r"^(?P<head>.*?) S(?P<season>\d+)E(?P<ep>\d+) - (?P<title>.*)$")


def log(msg):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


# ---------------------------------------------------------------- matching

def norm(s):
    """Lowercase, fold full-width characters, keep only letters and digits.
    Makes 'A⧸B ＂x＂ ｜ y' and 'A/B "x" | y' compare equal."""
    s = unicodedata.normalize("NFKC", s or "").casefold()
    return "".join(ch for ch in s if ch.isalnum())


def match_entries(files, entries):
    """files:   [{'path','ep','title'}]
    entries: [{'index','id','title'}] (playlist order, 1-based)
    Returns (matches, unmatched) where matches maps path -> (entry, how)."""
    used = set()
    matches = {}
    ordered = sorted(files, key=lambda f: f["ep"] or 0)

    by_norm = {}
    for e in entries:
        if e["id"]:
            by_norm.setdefault(norm(e["title"]), []).append(e)

    # pass 1: same title (ignoring punctuation / width / case)
    for f in ordered:
        cands = [e for e in by_norm.get(norm(f["title"]), []) if e["index"] not in used]
        if cands:
            e = min(cands, key=lambda e: abs(e["index"] - (f["ep"] or 0)))
            used.add(e["index"])
            matches[f["path"]] = (e, "title")

    # pass 2: one title is a prefix of the other (long names get truncated)
    for f in ordered:
        if f["path"] in matches:
            continue
        nf = norm(f["title"])
        if len(nf) < 20:
            continue
        cands = []
        for e in entries:
            ne = norm(e["title"])
            if e["id"] and e["index"] not in used and len(ne) >= 20 and (
                ne.startswith(nf) or nf.startswith(ne)
            ):
                cands.append(e)
        if cands:
            e = min(cands, key=lambda e: abs(e["index"] - (f["ep"] or 0)))
            used.add(e["index"])
            matches[f["path"]] = (e, "title-prefix")

    # pass 3: same position and reasonably similar title (renamed videos)
    for f in ordered:
        if f["path"] in matches:
            continue
        e = next((e for e in entries if e["index"] == f["ep"]), None)
        if e and e["id"] and e["index"] not in used:
            ratio = SequenceMatcher(None, norm(f["title"]), norm(e["title"])).ratio()
            if ratio >= 0.6:
                used.add(e["index"])
                matches[f["path"]] = (e, f"position+{int(ratio * 100)}%")

    unmatched = [f for f in ordered if f["path"] not in matches]
    return matches, unmatched


# ------------------------------------------------------------------ probing

def probe(path):
    r = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0",
         "-show_entries", "stream=width,height:format=duration",
         "-of", "json", path],
        capture_output=True, text=True, timeout=180,
    )
    data = json.loads(r.stdout or "{}")
    s = (data.get("streams") or [{}])[0]
    w = int(s.get("width") or 0)
    h = int(s.get("height") or 0)
    dur = float((data.get("format") or {}).get("duration") or 0)
    return w, h, dur


def short_side(w, h):
    return min(w, h) if w and h else 0


# ------------------------------------------------------------------ yt-dlp

def ydl_opts(outtmpl=None):
    opts = {
        "format": format_string(),
        "merge_output_format": "mp4",
        "quiet": True,
        "noprogress": True,
        "retries": 10,
        "fragment_retries": 10,
        "noplaylist": True,
    }
    if outtmpl:
        opts["outtmpl"] = outtmpl
    return opts


def fetch_entries(url):
    from yt_dlp import YoutubeDL
    opts = {"extract_flat": "in_playlist", "quiet": True, "skip_download": True}
    with YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=False)
    entries = []
    for i, e in enumerate(info.get("entries") or [], start=1):
        if not e:
            entries.append({"index": i, "id": None, "title": ""})
        else:
            entries.append({"index": i, "id": e.get("id"), "title": e.get("title") or ""})
    return info.get("title") or "Playlist", entries


def best_available_short_side(video_url):
    from yt_dlp import YoutubeDL
    opts = ydl_opts()
    opts["skip_download"] = True
    with YoutubeDL(opts) as ydl:
        info = ydl.extract_info(video_url, download=False)
    return short_side(info.get("width") or 0, info.get("height") or 0)


def download(video_url, staging):
    from yt_dlp import YoutubeDL
    os.makedirs(staging, exist_ok=True)
    with YoutubeDL(ydl_opts(os.path.join(staging, "new.%(ext)s"))) as ydl:
        ydl.extract_info(video_url, download=True)
    files = [os.path.join(staging, n) for n in os.listdir(staging) if n.endswith(".mp4")]
    if not files:
        raise RuntimeError("download finished but no .mp4 was produced")
    return max(files, key=os.path.getsize)


def search_candidates(query, n=8):
    from yt_dlp import YoutubeDL
    opts = {"extract_flat": True, "quiet": True, "skip_download": True}
    with YoutubeDL(opts) as ydl:
        info = ydl.extract_info(f"ytsearch{n}:{query}", download=False)
    out = []
    for e in info.get("entries") or []:
        if e and e.get("id"):
            out.append({
                "id": e["id"],
                "title": e.get("title") or "",
                "duration": e.get("duration") or 0,
                "channel": e.get("channel") or e.get("uploader") or "",
            })
    return out


def pick_candidate(local_title, local_dur, cands):
    """Returns (best_or_None, scored). A candidate is only accepted when its
    length is within 5% (or 10 s) of the local file AND its title is close."""
    nl = norm(local_title)
    scored = []
    for c in cands:
        nt = norm(c["title"])
        sim = SequenceMatcher(None, nl, nt).ratio() if nl and nt else 0.0
        if nl and nt and min(len(nl), len(nt)) >= 12 and (nl in nt or nt in nl):
            sim = max(sim, 0.9)
        dur_ok = bool(local_dur and c["duration"]) and \
            abs(c["duration"] - local_dur) <= max(10.0, 0.05 * local_dur)
        scored.append((sim, dur_ok, c))
    ok = [t for t in scored if t[1] and t[0] >= 0.75]
    best = max(ok, key=lambda t: t[0])[2] if ok else None
    return best, scored


def is_permanent(msg):
    """True when the error means the video is gone/blocked rather than a transient problem."""
    m = (msg or "").lower()
    if any(r in m for r in RETRYABLE):
        return False
    return any(p in m for p in PERMANENT)


def with_retries(fn):
    for attempt in (1, 2, 3):
        try:
            return fn()
        except Exception as exc:  # noqa: BLE001
            msg = str(exc).lower()
            if attempt < 3 and any(m in msg for m in RETRYABLE):
                wait = 20 * attempt
                log(f"    transient error ({str(exc)[:80]}), retrying in {wait}s")
                time.sleep(wait)
                continue
            raise


# ------------------------------------------------------------------ upgrading

def check_writable(directory):
    """Really try to create (and remove) a file - permission bits alone can lie on shared filesystems."""
    probe_path = os.path.join(directory, f".write-test-{os.getpid()}")
    try:
        with open(probe_path, "wb"):
            pass
        os.remove(probe_path)
    except OSError as exc:
        raise PermissionError(f"{directory}: {exc}") from exc


def verify_writable(paths):
    """True if every folder holding these files accepts new files. Logs what to fix if not."""
    dirs = sorted({os.path.dirname(p) for p in paths})
    bad = []
    for d in dirs:
        try:
            check_writable(d)
        except PermissionError as exc:
            bad.append(str(exc))
    if bad:
        log(f"NOT WRITABLE as uid {os.getuid()} / gid {os.getgid()} - upgrades would fail at the final swap:")
        for b in bad:
            log(f"  {b[:220]}")
        log("Fix ownership/permissions on those folders, then re-run.")
        return False
    log(f"Write check OK in {len(dirs)} folder(s)")
    return True


def swap_in(new_path, old_path):
    """Copy next to the old file under a hidden temp name, then atomically
    rename over it. The old file is only touched by the final rename."""
    tmp = os.path.join(os.path.dirname(old_path), "." + os.path.basename(old_path) + ".upgrade-tmp")
    try:
        shutil.copyfile(new_path, tmp)
        os.replace(tmp, old_path)
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)


def upgrade_one(old_path, video_id_or_url, tag):
    """Returns (status, detail, downloaded). status: upgraded | skipped"""
    video_url = video_id_or_url
    if not video_url.startswith("http"):
        video_url = f"https://www.youtube.com/watch?v={video_url}"

    ow, oh, odur = probe(old_path)
    old_s = short_side(ow, oh)

    best_s = with_retries(lambda: best_available_short_side(video_url))
    if best_s and best_s <= old_s:
        return "skipped", f"YouTube's best is {best_s}p, current is already {old_s}p", False

    staging = os.path.join(STAGING_ROOT, str(os.getpid()), tag)
    try:
        new_path = with_retries(lambda: download(video_url, staging))
        nw, nh, ndur = probe(new_path)
        new_s = short_side(nw, nh)
        if new_s <= old_s:
            return "skipped", f"new file is {new_s}p, not better than {old_s}p", True
        if odur and ndur and abs(ndur - odur) > max(10.0, 0.05 * odur):
            return "skipped", (f"duration mismatch (old {odur:.0f}s, new {ndur:.0f}s) - "
                               "wrong video or re-edited, old file kept"), True
        swap_in(new_path, old_path)
        return "upgraded", f"{ow}x{oh} -> {nw}x{nh}", True
    finally:
        shutil.rmtree(staging, ignore_errors=True)


# ------------------------------------------------------------------ discovery

def resolve_dir(root, query):
    names = sorted(n for n in os.listdir(root)
                   if not n.startswith(".") and os.path.isdir(os.path.join(root, n)))
    exact = [n for n in names if n == query]
    if exact:
        return os.path.join(root, exact[0])
    hits = [n for n in names if query.casefold() in n.casefold()]
    if len(hits) == 1:
        return os.path.join(root, hits[0])
    sys.exit(f"--show '{query}' matched {len(hits)} folders: {hits or 'none'}")


def list_episodes(show_dir):
    files = []
    for dirpath, dirnames, filenames in os.walk(show_dir):
        dirnames[:] = [d for d in dirnames if not d.startswith(".")]
        for n in filenames:
            if n.startswith(".") or not n.lower().endswith(".mp4"):
                continue
            stem = n[:-4]
            m = EP_RE.match(stem)
            files.append({
                "path": os.path.join(dirpath, n),
                "ep": int(m.group("ep")) if m else None,
                "title": m.group("title") if m else stem,
            })
    return files


def pace(delay):
    time.sleep(random.uniform(delay, delay * 1.5))


# ---------------------------------------------------------------------- main

def run_playlist(args):
    show_dir = resolve_dir(SHOWS_DIR, args.show)
    log(f"Show folder: {os.path.basename(show_dir)}")

    files = list_episodes(show_dir)
    log(f"Probing {len(files)} local files...")
    candidates = []
    for f in files:
        w, h, _ = probe(f["path"])
        f["short"] = short_side(w, h)
        if f["short"] < args.below:
            candidates.append(f)
    log(f"{len(candidates)} of {len(files)} files are below {args.below}p")
    if not candidates:
        return

    title, entries = fetch_entries(args.url)
    log(f"Playlist '{title}' has {len(entries)} entries")
    matches, unmatched = match_entries(candidates, entries)

    log(f"Matched {len(matches)}, unmatched {len(unmatched)}")
    for f in sorted(candidates, key=lambda f: f["ep"] or 0):
        if f["path"] in matches:
            e, how = matches[f["path"]]
            log(f"  E{f['ep']:>3} {f['short']:>4}p  [{how}]  {e['id']}  {e['title'][:60]}")
    for f in unmatched:
        log(f"  E{f['ep'] or 0:>3} {f['short']:>4}p  NO MATCH  {f['title'][:70]}")

    writable = verify_writable([f["path"] for f in candidates])

    if not args.apply:
        log("DRY RUN - nothing downloaded or changed. Re-run with --apply to upgrade.")
        return
    if not writable:
        log("Stopping before any download, since nothing could be saved.")
        return

    todo = sorted(matches.items(), key=lambda kv: next(f["ep"] or 0 for f in candidates if f["path"] == kv[0]))
    if args.limit:
        todo = todo[: args.limit]

    results = {"upgraded": [], "skipped": [], "error": []}
    consecutive_errors = 0
    for n, (path, (entry, how)) in enumerate(todo, start=1):
        label = os.path.basename(path)
        log(f"[{n}/{len(todo)}] {label[:80]}")
        downloaded = True
        try:
            status, detail, downloaded = upgrade_one(path, entry["id"], f"{n:03d}")
            consecutive_errors = 0
        except Exception as exc:  # noqa: BLE001
            msg = str(exc)
            if is_permanent(msg):
                status, detail = "skipped", f"video unavailable on YouTube, old file kept ({msg[:90]})"
                downloaded = False
                consecutive_errors = 0
            else:
                status, detail = "error", msg[:200]
                consecutive_errors += 1
        results[status].append((label, detail))
        log(f"    {status.upper()}: {detail}")
        if consecutive_errors >= 3:
            log("3 errors in a row - stopping so we don't hammer YouTube. Re-run later; "
                "already-upgraded files are skipped automatically.")
            break
        if n < len(todo):
            if downloaded:
                pace(args.delay)                    # be gentle after real downloads
            else:
                time.sleep(random.uniform(2, 4))    # nothing downloaded, short pause is plenty

    log(f"DONE: {len(results['upgraded'])} upgraded, {len(results['skipped'])} skipped, "
        f"{len(results['error'])} errors, {len(unmatched)} unmatched (never touched)")
    for label, detail in results["skipped"] + results["error"]:
        log(f"  not upgraded: {label[:60]} -> {detail}")


def run_single(args):
    hits = []
    for dirpath, dirnames, filenames in os.walk(DOWNLOAD_DIR):
        dirnames[:] = [d for d in dirnames if not d.startswith(".")]
        for n in filenames:
            if n.lower().endswith(".mp4") and not n.startswith(".") \
                    and args.file.casefold() in n.casefold():
                hits.append(os.path.join(dirpath, n))
    if len(hits) != 1:
        sys.exit(f"--file '{args.file}' matched {len(hits)} files: {hits or 'none'}")
    path = hits[0]
    w, h, dur = probe(path)
    log(f"File: {os.path.basename(path)}  ({w}x{h}, {dur:.0f}s)")
    if not args.url:
        stem = os.path.splitext(os.path.basename(path))[0]
        log(f"Searching YouTube for: {stem}")
        cands = with_retries(lambda: search_candidates(stem))
        best, scored = pick_candidate(stem, dur, cands)
        for sim, dur_ok, c in sorted(scored, key=lambda t: -t[0]):
            log(f"  candidate {c['id']}  {int(c['duration'] or 0):>5}s  title {int(sim * 100):>3}%  "
                f"{'length OK' if dur_ok else 'length differs'}  {c['channel'][:18]} | {c['title'][:55]}")
        if not best:
            log("NO CONFIDENT MATCH - nothing changed. Find the video yourself and pass --url instead.")
            return
        args.url = f"https://www.youtube.com/watch?v={best['id']}"
        log(f"Best match: {args.url}  ({best['title'][:70]})")
    writable = verify_writable([path])
    if not args.apply:
        log("DRY RUN - nothing downloaded or changed. Re-run with --apply to upgrade.")
        return
    if not writable:
        log("Stopping before any download, since nothing could be saved.")
        return
    try:
        status, detail, _ = upgrade_one(path, args.url, "single")
    except Exception as exc:  # noqa: BLE001
        msg = str(exc)
        status = "skipped" if is_permanent(msg) else "error"
        detail = ("video unavailable on YouTube, old file kept - " if status == "skipped" else "") + msg[:200]
    log(f"{status.upper()}: {detail}")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--show", help="show folder name under the shows library (substring is fine)")
    ap.add_argument("--file", help="single video in the downloads folder (substring of filename)")
    ap.add_argument("--url", help="playlist URL (with --show) or video URL (with --file)")
    ap.add_argument("--search", action="store_true", help="with --file and no --url: find the video on YouTube by title")
    ap.add_argument("--max-height", type=int, default=1080, help="resolution cap as a HEIGHT in pixels (default 1080)")
    ap.add_argument("--apply", action="store_true", help="really download and replace (default: dry run)")
    ap.add_argument("--below", type=int, default=1000, help="upgrade files whose short side is below this (default 1000)")
    ap.add_argument("--limit", type=int, default=0, help="only process the first N matches (for a trial run)")
    ap.add_argument("--delay", type=float, default=20, help="seconds to pause between videos (default 20, jittered up to 1.5x)")
    args = ap.parse_args()

    if bool(args.show) == bool(args.file):
        sys.exit("give exactly one of --show or --file")
    if args.show and not args.url:
        sys.exit("--show needs --url (the playlist URL)")
    if args.file and not args.url and not args.search:
        sys.exit("--file needs --url (the video URL) or --search")
    global MAX_HEIGHT
    MAX_HEIGHT = args.max_height
    if args.show:
        run_playlist(args)
    else:
        run_single(args)


if __name__ == "__main__":
    main()
