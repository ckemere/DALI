"""
Standalone compilation worker process.

Run this separately from the Flask web app:
    python compile_worker_main.py

It connects to Redis and processes compilation jobs from the queue.
"""

import os
import time
import logging
from compile_queue import CompilationQueue

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [worker]: %(message)s",
    handlers=[logging.FileHandler("compile_worker.log"), logging.StreamHandler()],
)

redis_host = os.environ.get("REDIS_HOST", "localhost")
redis_port = int(os.environ.get("REDIS_PORT", "6379"))
max_workers = int(os.environ.get("COMPILE_WORKERS", "8"))

logging.info("Starting compile worker (redis=%s:%d, workers=%d)", redis_host, redis_port, max_workers)

queue = CompilationQueue(redis_host=redis_host, redis_port=redis_port)
queue.start_workers(max_workers=max_workers)

# Keep main thread alive
while True:
    time.sleep(60)
