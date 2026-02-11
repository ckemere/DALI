"""
Compilation Queue System
- Redis-backed job queue
- Threaded workers that run real compilations via makefile_generator
- Queue position tracking
- Heartbeat + reaper for stale jobs
- Job cancellation
- PCB DRC support via pcb_makefile_generator
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

from makefile_generator import (
    create_makefile_for_lab,
    ensure_linker_script,
    verify_toolchain,
)

from pcb_makefile_generator import (
    create_makefile_for_pcb,
    verify_pcb_toolchain,
)

TEMPLATE_FOLDER = os.environ.get("TEMPLATE_FOLDER", "template_files")


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
        self.max_workers = int(os.environ.get("COMPILE_WORKERS", "8"))

    def is_available(self):
        return self.redis is not None

    # -------------------------------------------------------------------------
    # WORKERS
    # -------------------------------------------------------------------------

    def start_workers(self, max_workers=None):
        if not self.redis:
            raise RuntimeError("Redis unavailable")

        if max_workers is not None:
            self.max_workers = max_workers

        # Verify embedded C toolchain
        ok, msg = verify_toolchain()
        if ok:
            logging.info("Embedded C toolchain verified: %s", msg)
        else:
            logging.warning("Embedded C toolchain not available: %s", msg)

        # Verify PCB toolchain
        ok_pcb, msg_pcb = verify_pcb_toolchain()
        if ok_pcb:
            logging.info("PCB toolchain verified: %s", msg_pcb)
        else:
            logging.warning("PCB toolchain not available: %s", msg_pcb)

        self.executor = ThreadPoolExecutor(max_workers=self.max_workers)
        for _ in range(self.max_workers):
            self.executor.submit(self._worker)
        threading.Thread(target=self._reaper, daemon=True).start()
        logging.info("Started %d compile workers", self.max_workers)

    # -------------------------------------------------------------------------
    # QUEUE API
    # -------------------------------------------------------------------------

    def submit_job(self, **meta):
        job_id = str(uuid.uuid4())

        # Serialize any non-string values (lab_config may be a dict)
        for key, val in meta.items():
            if not isinstance(val, str):
                meta[key] = json.dumps(val)

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
        if not data:
            return None

        # Add queue position if still queued
        if data.get("status") == "queued":
            queue_items = self.redis.lrange("compile_queue", 0, -1)
            try:
                data["position"] = queue_items.index(job_id) + 1
            except ValueError:
                data["position"] = 0
            data["estimated_wait"] = data["position"] * 10  # rough estimate

        # Deserialize result JSON if present
        if data.get("result"):
            try:
                data["result"] = json.loads(data["result"])
            except (json.JSONDecodeError, TypeError):
                pass

        return data

    def cancel_job(self, job_id, student_id=None):
        """Cancel a queued job. Only the submitting student can cancel."""
        data = self.redis.hgetall(f"job:{job_id}")
        if not data:
            return {"success": False, "error": "Job not found"}

        if student_id and data.get("student_id") != student_id:
            return {"success": False, "error": "Not authorized to cancel this job"}

        if data.get("status") != "queued":
            return {"success": False, "error": f"Cannot cancel job in state: {data.get('status')}"}

        # Remove from queue
        self.redis.lrem("compile_queue", 1, job_id)
        self.redis.hset(f"job:{job_id}", mapping={
            "status": "cancelled",
            "completed_at": datetime.utcnow().isoformat(),
            "result": json.dumps({"success": False, "error": "Cancelled by user"}),
        })

        # Clean up build dir
        build_dir = data.get("build_dir", "")
        if build_dir and os.path.isdir(build_dir):
            shutil.rmtree(build_dir, ignore_errors=True)

        return {"success": True}

    def get_full_queue(self):
        """Return all active (queued + compiling) jobs for the admin dashboard."""
        jobs = []

        # Queued jobs (in order)
        queue_items = self.redis.lrange("compile_queue", 0, -1)
        for i, job_id in enumerate(queue_items):
            data = self.redis.hgetall(f"job:{job_id}")
            if data:
                data["position"] = i + 1
                data["state"] = "queued"
                jobs.append(data)

        # Currently compiling jobs
        for job_id in self.redis.smembers("compile_active"):
            data = self.redis.hgetall(f"job:{job_id}")
            if data:
                data["state"] = "compiling"
                data["position"] = 0
                jobs.append(data)

        return jobs

    # -------------------------------------------------------------------------
    # WORKER LOOP
    # -------------------------------------------------------------------------

    def _worker(self):
        while not self._stop.is_set():
            item = self.redis.blpop("compile_queue", timeout=5)
            if not item:
                continue

            _, job_id = item
            meta = self.redis.hgetall(f"job:{job_id}")

            # Skip if cancelled while waiting
            if meta.get("status") == "cancelled":
                continue

            self.redis.sadd("compile_active", job_id)
            self.redis.hset(f"job:{job_id}", mapping={
                "status": "compiling",
                "started_at": datetime.utcnow().isoformat(),
            })

            # Start heartbeat thread
            hb_stop = threading.Event()
            threading.Thread(
                target=self._heartbeat, args=(job_id, hb_stop), daemon=True
            ).start()

            try:
                result = self._run_compilation(job_id, meta)
            except Exception as e:
                logging.exception("Compilation crashed for job %s", job_id)
                result = {"success": False, "error": str(e), "stdout": "", "stderr": ""}

            hb_stop.set()

            self.redis.hset(f"job:{job_id}", mapping={
                "status": "complete" if result["success"] else "failed",
                "completed_at": datetime.utcnow().isoformat(),
                "result": json.dumps(result),
            })
            self.redis.srem("compile_active", job_id)

    def _run_compilation(self, job_id, meta):
        """
        Dispatch to the correct build pipeline based on lab type.
        """
        build_dir = meta.get("build_dir", "")
        if not build_dir or not os.path.isdir(build_dir):
            return {
                "success": False,
                "error": "Build directory not found. Please try again.",
                "stdout": "",
                "stderr": "",
            }

        # Parse lab config to determine type
        lab_config_raw = meta.get("lab_config", "{}")
        try:
            lab_config = json.loads(lab_config_raw)
        except (json.JSONDecodeError, TypeError):
            lab_config = {}

        lab_type = lab_config.get("type", "embedded_c")

        if lab_type == "kicad_pcb":
            return self._run_pcb_drc(job_id, meta, build_dir, lab_config)
        else:
            return self._run_embedded_c(job_id, meta, build_dir, lab_config)

    def _run_embedded_c(self, job_id, meta, build_dir, lab_config):
        """
        Compile embedded C code (original behavior).
        """
        try:
            lab_name = meta.get("lab_name", "")
            template_dir = os.path.join(TEMPLATE_FOLDER, lab_name)
            display_name = lab_config.get("display_name", "firmware")
            output_name = display_name.replace(" ", "_")

            source_files = [f for f in os.listdir(build_dir) if f.endswith(".c")]

            logging.info(
                "Job %s: compiling %d source files in %s",
                job_id, len(source_files), build_dir,
            )

            create_makefile_for_lab(build_dir, source_files, output_name)
            ensure_linker_script(build_dir, template_dir)

            proc = subprocess.run(
                ["make", "-C", build_dir, "all"],
                capture_output=True,
                text=True,
                timeout=self.max_runtime,
                env={**os.environ, "PATH": os.environ.get("PATH", "/usr/bin")},
            )

            success = proc.returncode == 0

            logging.info(
                "Job %s: compilation %s (return code %d)",
                job_id,
                "succeeded" if success else "failed",
                proc.returncode,
            )

            return {
                "success": success,
                "return_code": proc.returncode,
                "stdout": proc.stdout,
                "stderr": proc.stderr,
            }

        except subprocess.TimeoutExpired:
            logging.warning("Job %s: compilation timed out after %ds", job_id, self.max_runtime)
            return {
                "success": False,
                "error": f"Compilation timed out after {self.max_runtime} seconds.",
                "stdout": "",
                "stderr": "",
            }
        except FileNotFoundError as e:
            logging.error("Job %s: %s", job_id, e)
            return {
                "success": False,
                "error": str(e),
                "stdout": "",
                "stderr": "",
            }
        finally:
            if build_dir and os.path.isdir(build_dir):
                shutil.rmtree(build_dir, ignore_errors=True)

    def _run_pcb_drc(self, job_id, meta, build_dir, lab_config):
        """
        Run KiCad DRC + preview generation for a PCB assignment.

        Steps:
          1. Find the .kicad_pcb file in the build directory
          2. Generate a Makefile for DRC + SVG export + PNG conversion
          3. Run make
          4. Copy results (HTML reports, JSON, PNGs) back to student folder
          5. Clean up build directory
        """
        try:
            # Find the .kicad_pcb file
            pcb_files = [f for f in os.listdir(build_dir) if f.endswith(".kicad_pcb")]
            if not pcb_files:
                return {
                    "success": False,
                    "error": "No .kicad_pcb file found.",
                    "stdout": "",
                    "stderr": "",
                }

            pcb_filename = pcb_files[0]
            dru_files = lab_config.get("dru_files", [])

            logging.info(
                "Job %s: running PCB DRC on %s with %d rule sets",
                job_id, pcb_filename, len(dru_files),
            )

            # Generate Makefile
            create_makefile_for_pcb(build_dir, pcb_filename, dru_files)

            # Run make
            proc = subprocess.run(
                ["make", "-C", build_dir, "all"],
                capture_output=True,
                text=True,
                timeout=self.max_runtime,
                env={**os.environ, "PATH": os.environ.get("PATH", "/usr/bin")},
            )

            logging.info(
                "Job %s: PCB DRC completed (return code %d)",
                job_id, proc.returncode,
            )

            # Copy results back to student folder
            student_folder = meta.get("student_folder", "")
            if student_folder:
                results_dir = os.path.join(student_folder, "_pcb_results")
                # Clear old results
                if os.path.isdir(results_dir):
                    shutil.rmtree(results_dir)
                os.makedirs(results_dir, exist_ok=True)

                for fname in os.listdir(build_dir):
                    if fname.startswith("drc_") and fname.endswith((".html", ".json")):
                        shutil.copy2(
                            os.path.join(build_dir, fname),
                            os.path.join(results_dir, fname),
                        )
                    elif fname.startswith("preview_") and fname.endswith(".png"):
                        shutil.copy2(
                            os.path.join(build_dir, fname),
                            os.path.join(results_dir, fname),
                        )

            # For PCB, "success" means the pipeline ran — individual DRC
            # pass/fail is in the reports. We report success=True unless
            # the make process itself crashed.
            return {
                "success": True,
                "return_code": proc.returncode,
                "stdout": proc.stdout,
                "stderr": proc.stderr,
            }

        except subprocess.TimeoutExpired:
            logging.warning("Job %s: PCB DRC timed out after %ds", job_id, self.max_runtime)
            return {
                "success": False,
                "error": f"DRC timed out after {self.max_runtime} seconds.",
                "stdout": "",
                "stderr": "",
            }
        except FileNotFoundError as e:
            logging.error("Job %s: %s", job_id, e)
            return {
                "success": False,
                "error": str(e),
                "stdout": "",
                "stderr": "",
            }
        finally:
            if build_dir and os.path.isdir(build_dir):
                shutil.rmtree(build_dir, ignore_errors=True)

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
                    self._fail(job_id, "Stale heartbeat — worker may have crashed")
            time.sleep(5)

    def _fail(self, job_id, reason):
        logging.warning("Reaper failing job %s: %s", job_id, reason)

        meta = self.redis.hgetall(f"job:{job_id}")
        build_dir = meta.get("build_dir", "")
        if build_dir and os.path.isdir(build_dir):
            shutil.rmtree(build_dir, ignore_errors=True)

        self.redis.hset(f"job:{job_id}", mapping={
            "status": "failed",
            "completed_at": datetime.utcnow().isoformat(),
            "result": json.dumps({
                "success": False,
                "error": reason,
                "stdout": "",
                "stderr": "",
            }),
        })
        self.redis.srem("compile_active", job_id)
