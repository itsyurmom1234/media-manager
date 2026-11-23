import os
import uuid

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from redis import Redis
from rq import Queue

from app.db import init_db, get_session, Job, Playlist
from app.tasks import run_download
from app.playlist import probe_playlist

REDIS_URL = os.environ.get("REDIS_URL", "redis://redis:6379/0")

# Short, stable, iconic video used for the self-test - "Me at the zoo" is
# the first video ever uploaded to YouTube, 19 seconds, extremely unlikely
# to ever be taken down. Reused for both the standalone and playlist legs
# of the test so it doesn't depend on any external playlist staying valid.
TEST_VIDEO_URL = "https://www.youtube.com/watch?v=jNQXAC9IVRw"

app = FastAPI(title="Download service")
redis_conn = Redis.from_url(REDIS_URL)
queue = Queue("downloads", connection=redis_conn)


@app.on_event("startup")
def on_startup():
    init_db()


class NewJob(BaseModel):
    url: str


class SelfTestCleanup(BaseModel):
    job_id: str
    playlist_id: str


def _job_dict(job: Job) -> dict:
    return {
        "id": job.id,
        "url": job.url,
        "title": job.title,
        "status": job.status,
        "file_path": job.file_path,
        "error": job.error,
        "playlist_index": job.playlist_index,
        "created_at": job.created_at.isoformat() if job.created_at else None,
    }


@app.post("/jobs")
def create_job(payload: NewJob):
    url = payload.url.strip()
    probed = probe_playlist(url)

    session = get_session()

    if probed is None:
        job = Job(id=str(uuid.uuid4()), url=url, status="queued")
        session.add(job)
        session.commit()
        queue.enqueue(run_download, job.id, job.url, job_id=job.id)
        result = {"type": "job", **_job_dict(job)}
        session.close()
        return result

    title, items = probed
    playlist = Playlist(
        id=str(uuid.uuid4()), title=title, url=url, total_count=len(items)
    )
    session.add(playlist)
    session.flush()

    jobs_out = []
    to_enqueue = []
    for index, item in enumerate(items, start=1):
        job = Job(
            id=str(uuid.uuid4()),
            url=item["url"] or "",
            title=item.get("title"),
            status="queued" if item["url"] else "failed",
            error=None if item["url"] else "Video unavailable",
            playlist_id=playlist.id,
            playlist_index=index,
        )
        session.add(job)
        session.flush()
        if item["url"]:
            to_enqueue.append((job.id, job.url))
        jobs_out.append(_job_dict(job))

    session.commit()

    # Only enqueue once the transaction is actually committed - otherwise
    # the worker can dequeue a job and query Postgres for it before this
    # row is visible, since it runs in a completely separate process.
    for job_id, job_url in to_enqueue:
        queue.enqueue(run_download, job_id, job_url, job_id=job_id)

    result = {
        "type": "playlist",
        "id": playlist.id,
        "title": playlist.title,
        "url": playlist.url,
        "total_count": playlist.total_count,
        "created_at": playlist.created_at.isoformat(),
        "jobs": jobs_out,
    }
    session.close()
    return result


@app.post("/self-test")
def run_self_test():
    """Runs one real video + one real 2-video playlist through the actual
    pipeline. Nothing is deleted here - the caller polls /jobs until both
    reach a terminal state, then calls /self-test/cleanup to tear down."""
    session = get_session()

    job = Job(id=str(uuid.uuid4()), url=TEST_VIDEO_URL, status="queued")
    session.add(job)
    session.commit()
    queue.enqueue(run_download, job.id, job.url, job_id=job.id)
    job_id = job.id

    playlist = Playlist(
        id=str(uuid.uuid4()),
        title="Self-Test Playlist",
        url=TEST_VIDEO_URL,
        total_count=2,
    )
    session.add(playlist)
    session.flush()

    to_enqueue = []
    for index in (1, 2):
        pjob = Job(
            id=str(uuid.uuid4()),
            url=TEST_VIDEO_URL,
            title="Me at the zoo",
            status="queued",
            playlist_id=playlist.id,
            playlist_index=index,
        )
        session.add(pjob)
        session.flush()
        to_enqueue.append((pjob.id, pjob.url))

    session.commit()
    for pjob_id, pjob_url in to_enqueue:
        queue.enqueue(run_download, pjob_id, pjob_url, job_id=pjob_id)

    playlist_id = playlist.id
    session.close()
    return {"job_id": job_id, "playlist_id": playlist_id}


@app.post("/self-test/cleanup")
def cleanup_self_test(payload: SelfTestCleanup):
    """Deletes the self-test's downloaded file(s), its show/season folder,
    and its job/playlist rows. Safe to call even if some pieces are
    already gone."""
    session = get_session()
    removed = []

    job = session.get(Job, payload.job_id)
    if job:
        if job.file_path and os.path.exists(job.file_path):
            os.remove(job.file_path)
            removed.append(job.file_path)
        session.delete(job)

    playlist = session.get(Playlist, payload.playlist_id)
    if playlist:
        children = session.query(Job).filter(Job.playlist_id == playlist.id).all()
        show_folder = None
        for child in children:
            if child.file_path:
                if os.path.exists(child.file_path):
                    os.remove(child.file_path)
                    removed.append(child.file_path)
                nfo_path = os.path.splitext(child.file_path)[0] + ".nfo"
                if os.path.exists(nfo_path):
                    os.remove(nfo_path)
                    removed.append(nfo_path)
                if show_folder is None:
                    # file_path is .../Show Name/Season 01/file.ext
                    show_folder = os.path.dirname(os.path.dirname(child.file_path))
            session.delete(child)
        session.delete(playlist)

        if show_folder and os.path.isdir(show_folder):
            import shutil
            shutil.rmtree(show_folder, ignore_errors=True)

    session.commit()
    session.close()
    return {"cleaned": True, "removed_files": removed}


@app.get("/jobs/{job_id}")
def get_job(job_id: str):
    session = get_session()
    job = session.get(Job, job_id)
    session.close()
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found")
    return {"type": "job", **_job_dict(job)}


@app.delete("/jobs")
def clear_jobs():
    """Clears job/playlist history AND empties the Redis queue, so no
    already-queued jobs survive to later find their database row gone.
    Does NOT touch any downloaded files on disk - only tracking records."""
    session = get_session()
    session.query(Job).delete()
    session.query(Playlist).delete()
    session.commit()
    session.close()
    queue.empty()
    return {"cleared": True}


@app.get("/jobs")
def list_jobs():
    session = get_session()

    playlists = session.query(Playlist).order_by(Playlist.created_at.desc()).limit(50).all()
    standalone_jobs = (
        session.query(Job)
        .filter(Job.playlist_id.is_(None))
        .order_by(Job.created_at.desc())
        .limit(50)
        .all()
    )

    items = []
    for p in playlists:
        children = (
            session.query(Job)
            .filter(Job.playlist_id == p.id)
            .order_by(Job.playlist_index.asc())
            .all()
        )
        items.append(
            {
                "type": "playlist",
                "id": p.id,
                "title": p.title,
                "url": p.url,
                "total_count": p.total_count,
                "created_at": p.created_at.isoformat() if p.created_at else None,
                "jobs": [_job_dict(j) for j in children],
            }
        )
    for j in standalone_jobs:
        items.append({"type": "job", **_job_dict(j)})

    items.sort(key=lambda x: x["created_at"] or "", reverse=True)
    session.close()
    return items


@app.get("/health")
def health():
    return {"status": "ok"}
