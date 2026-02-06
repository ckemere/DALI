import time
from compile_queue import CompilationQueue

queue = CompilationQueue()
queue.start_workers(max_workers=int(os.environ.get("COMPILE_WORKERS", "16")))

while True:
    time.sleep(60)

