import os
import time
import random
import datetime as dt
from xml.sax.saxutils import escape

from yt_dlp import YoutubeDL
from yt_dlp.utils import sanitize_filename

from app.db import get_session, Job, Playlist

DOWNLOAD_DIR = os.environ.get("DOWNLOAD_DIR", "/downloads")
SHOWS_DIR = os.environ.get("SHOWS_DIR", "/shows")

MAX_ATTEMPTS = 3
RETRYABLE_MARKERS = (
    "403", "forbidden", "429", "too many requests", "503", "service unavailable",
)


def _write_nfo(file_path: str, job: Job, info: dict):
    nfo_path = os.path.splitext(file_path)[0] + ".nfo"
    title = info.get("title") or job.title or ""
    plot = info.get("description") or ""
    upload_date = info.get("upload_date")  # YYYYMMDD
    aired = ""
    if upload_date and len(upload_date) == 8:
        aired = f"{upload_date[:4]}-{upload_date[4:6]}-{upload_date[6:8]}"

    xml = f'''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<episodedetails>
  <title>{escape(title)}</title>
  <season>1</season>
  <episode>{job.playlist_index}</episode>
  <plot>{escape(plot)}</plot>
  <aired>{aired}</aired>
</episodedetails>
'''
    with open(nfo_path, "w", encoding="utf-8") as f:
        f.write(xml)


def _build_outtmpl(job: Job, session) -> str:
    """Playlist videos land in a Jellyfin-shaped show/season folder with
    zero-padded S01E## naming. Standalone downloads keep the plain title
    in the root download folder."""
    if not job.playlist_id:
        return os.path.join(DOWNLOAD_DIR, "%(title)s.%(ext)s")

    playlist = session.get(Playlist, job.playlist_id)
    total = playlist.total_count if playlist else 0
    width = max(2, len(str(total)))
    show_name = sanitize_filename(playlist.title) if playlist else "Playlist"
    episode_tag = f"S01E{str(job.playlist_index).zfill(width)}"

    season_dir = os.path.join(SHOWS_DIR, show_name, "Season 01")
    os.makedirs(season_dir, exist_ok=True)

    return os.path.join(season_dir, f"{show_name} {episode_tag} - %(title)s.%(ext)s")


def _run_make_posters():
    """Signals that the shows library has new content, by touching a
    trigger file inside SHOWS_DIR. We can't run the poster script
    directly from this container - its python3 is a symlink to the
    NAS host's own system python (/usr/bin/python3), which doesn't
    exist inside this (Debian-based) container. A host-side script,
    running natively on Unraid, watches for this file and does the
    actual work where that symlink resolves correctly."""
    trigger_path = os.path.join(SHOWS_DIR, ".posters-pending")
    try:
        with open(trigger_path, "w") as f:
            f.write(dt.datetime.utcnow().isoformat())
        print(f"[make_posters] trigger written: {trigger_path}", flush=True)
    except Exception as exc:  # noqa: BLE001
        print(f"[make_posters] failed to write trigger: {exc}", flush=True)


def run_download(job_id: str, url: str):
    """Executed by an RQ worker. Downloads the URL with yt-dlp and
    updates the job row so the API can report progress/results."""
    print(f"[trace] {job_id} start", flush=True)
    session = get_session()
    job = session.get(Job, job_id)
    if job is None:
        print(f"[trace] {job_id} no job row found, bailing", flush=True)
        session.close()
        return

    job.status = "running"
    job.updated_at = dt.datetime.utcnow()
    session.commit()
    print(f"[trace] {job_id} committed status=running", flush=True)

    # Small random delay so a big playlist doesn't fire requests back-to-back
    # in a way that looks like scraping to YouTube's rate limiter.
    delay = random.uniform(1, 3)
    print(f"[trace] {job_id} sleeping {delay:.2f}s", flush=True)
    time.sleep(delay)

    outtmpl = _build_outtmpl(job, session)
    print(f"[trace] {job_id} outtmpl={outtmpl}", flush=True)

    ydl_opts = {
        "outtmpl": outtmpl,
        "format": "bv*[height<=1080]+ba/b[height<=1080]/bv*+ba/b",
        "merge_output_format": "mp4",
        "quiet": True,
        "noprogress": True,
        "retries": 10,
        "fragment_retries": 10,
    }

    last_error = None
    for attempt in range(1, MAX_ATTEMPTS + 1):
        print(f"[trace] {job_id} attempt {attempt} starting yt-dlp", flush=True)
        try:
            with YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(url, download=True)
                file_path = ydl.prepare_filename(info)
            print(f"[trace] {job_id} yt-dlp finished, file_path={file_path}", flush=True)

            job.status = "done"
            job.file_path = file_path
            job.error = None
            last_error = None

            if job.playlist_id:
                try:
                    _write_nfo(file_path, job, info)
                    print(f"[trace] {job_id} nfo written", flush=True)
                except Exception as nfo_exc:
                    print(f"[trace] {job_id} nfo write failed: {type(nfo_exc).__name__}: {nfo_exc}", flush=True)
            break
        except Exception as exc:  # noqa: BLE001 - we want to record any failure reason
            import traceback
            print(f"[trace] {job_id} EXCEPTION on attempt {attempt}: {type(exc).__name__}: {exc}", flush=True)
            traceback.print_exc()
            last_error = str(exc) or f"{type(exc).__name__} (no message)"
            retryable = any(marker in last_error.lower() for marker in RETRYABLE_MARKERS)
            if retryable and attempt < MAX_ATTEMPTS:
                time.sleep(5 * attempt)  # 5s, then 10s
                continue
            job.status = "failed"
            job.error = last_error
            break

    print(f"[trace] {job_id} about to commit final status={job.status}", flush=True)
    job.updated_at = dt.datetime.utcnow()
    session.commit()
    print(f"[trace] {job_id} committed final status={job.status}", flush=True)

    if job.playlist_id:
        remaining = (
            session.query(Job)
            .filter(Job.playlist_id == job.playlist_id, Job.status.in_(("queued", "running")))
            .count()
        )
        print(f"[trace] {job_id} playlist {job.playlist_id} remaining={remaining}", flush=True)
        if remaining == 0:
            _run_make_posters()

    session.close()
    print(f"[trace] {job_id} session closed, done", flush=True)
