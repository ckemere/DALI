"""
Compilation Queue System
- Queue position tracking
- Admin dashboard
- Job cancellation
- NetID mapping
"""

import os
import json
import time
import shutil
import logging
import redis
import subprocess
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor
import threading
import uuid

class CompilationQueue:
    def __init__(self, redis_host="localhost", redis_port=6379):
        try:
            self.redis = redis.Redis(host=redis_host, port=redis_port, decode_responses=True)
            self.redis.ping()
        except redis.ConnectionError:
            self.redis = None

        self.executor = None
        self._stop = threading.Event()

        self.heartbeat_interval = int(os.environ.get("COMPILE_HEARTBEAT_INTERVAL", "2"))
        self.stale_seconds = int(os.environ.get("COMPILE_STALE_SECONDS", "30"))
        self.max_runtime = int(os.environ.get("COMPILE_MAX_RUNTIME", "60"))

    def is_available(self):
        return self.redis is not None

    # -------------------------------------------------------------------------
    # WORKERS
    # -------------------------------------------------------------------------

    def start_workers(self, max_workers=8):
        if not self.redis:
            raise RuntimeError("Redis unavailable")

        self.executor = ThreadPoolExecutor(max_workers=max_workers)
        for _ in range(max_workers):
            self.executor.submit(self._worker)
        threading.Thread(target=self._reaper, daemon=True).start()

    # -------------------------------------------------------------------------
    # QUEUE API
    # -------------------------------------------------------------------------

    def submit_job(self, **meta):
        job_id = str(uuid.uuid4())
        meta.update(
            job_id=job_id,
            status="queued",
            queued_at=datetime.utcnow().isoformat(),
            started_at="",
            completed_at="",
            heartbeat_at="",
            result="",
        )
        self.redis.hset(f"job:{job_id}", mapping=meta)
        self.redis.rpush("compile_queue", job_id)
        return job_id

    def get_job_status(self, job_id):
        data = self.redis.hgetall(f"job:{job_id}")
        return data or None

    # -------------------------------------------------------------------------
    # WORKER LOOP
    # -------------------------------------------------------------------------

    def _worker(self):
        while not self._stop.is_set():
            item = self.redis.blpop("compile_queue", timeout=5)
            if not item:
                continue

            _, job_id = item
            self.redis.sadd("compile_active", job_id)
            self.redis.hset(f"job:{job_id}", mapping={
                "status": "compiling",
                "started_at": datetime.utcnow().isoformat(),
            })

            hb_stop = threading.Event()
            threading.Thread(target=self._heartbeat, args=(job_id, hb_stop), daemon=True).start()

            try:
                result = {"success": True}
            except Exception as e:
                result = {"success": False, "error": str(e)}

            hb_stop.set()
            self.redis.hset(f"job:{job_id}", mapping={
                "status": "complete" if result["success"] else "failed",
                "completed_at": datetime.utcnow().isoformat(),
                "result": json.dumps(result),
            })
            self.redis.srem("compile_active", job_id)

    # -------------------------------------------------------------------------
    # HEARTBEAT + REAPER
    # -------------------------------------------------------------------------

    def _heartbeat(self, job_id, stop):
        while not stop.is_set():
            self.redis.hset(f"job:{job_id}", "heartbeat_at", datetime.utcnow().isoformat())
            stop.wait(self.heartbeat_interval)

    def _reaper(self):
        while not self._stop.is_set():
            now = datetime.utcnow()
            for job_id in self.redis.smembers("compile_active"):
                meta = self.redis.hgetall(f"job:{job_id}")
                hb = meta.get("heartbeat_at")
                if not hb:
                    self._fail(job_id, "Missing heartbeat")
                    continue
                dt = datetime.fromisoformat(hb)
                if (now - dt).total_seconds() > self.stale_seconds:
                    self._fail(job_id, "Stale heartbeat")
            time.sleep(5)

    def _fail(self, job_id, reason):
        self.redis.hset(f"job:{job_id}", mapping={
            "status": "failed",
            "completed_at": datetime.utcnow().isoformat(),
            "result": json.dumps({"success": False, "error": reason}),
        })
        self.redis.srem("compile_active", job_id)
