import os

from redis import Redis
from rq import SimpleWorker, Queue

from app.db import init_db

REDIS_URL = os.environ.get("REDIS_URL", "redis://redis:6379/0")

if __name__ == "__main__":
    init_db()
    conn = Redis.from_url(REDIS_URL)
    # SimpleWorker runs jobs in this same process instead of forking a
    # child per job. The default Worker's fork() breaks our SQLAlchemy/
    # psycopg2 connection (created once at import time in the parent) -
    # the child inherits it, and writes silently fail to persist even
    # though no exception is raised. We only run one worker container
    # processing sequentially anyway, so there's no downside here.
    worker = SimpleWorker([Queue("downloads", connection=conn)], connection=conn)
    worker.work()
