"""
DALI Load Test — Full Student Workflow

Simulates realistic student behavior:
  1. A couple of failed TLS handshakes (browser rejecting self-signed cert)
  2. Successful login
  3. Navigate to assignment page
  4. Upload each template code file (with a small .c file)
  5. Upload a writeup
  6. Trigger compilation and poll until complete
  7. Record timing for every step

Requirements:
    pip install locust

Usage:
    # First, generate test students and start DALI with that roster:
    #   python generate_test_students.py --output test_students.csv
    #   ROSTER_CSV_PATH=test_students.csv python app_complete.py
    #
    # Then run the load test:
    locust -f locustfile.py --host https://localhost:5000

    # Or for HTTP (no TLS):
    locust -f locustfile.py --host http://localhost:5000

    # Headless mode (no web UI):
    locust -f locustfile.py --host https://localhost:5000 \
           --users 50 --spawn-rate 5 --run-time 5m --headless

    # Open http://localhost:8089 for the Locust web UI

Environment variables:
    TEST_ROSTER_CSV    Path to the test students CSV (default: test_students.csv)
    ASSIGNMENT_ID      Canvas assignment ID to test against (default: 505415, Lab 3)
    LAB_NAME           Lab template directory name (default: lab3)
    SKIP_TLS_ABUSE     Set to "1" to skip the failed-handshake simulation
    TLS_FAILURES       Number of failed TLS handshakes per user (default: 2)
"""

import csv
import os
import io
import ssl
import socket
import time
import random
import logging
from itertools import cycle

from locust import HttpUser, task, between, events

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

ROSTER_CSV = os.environ.get("TEST_ROSTER_CSV", "test_students.csv")
ASSIGNMENT_ID = os.environ.get("ASSIGNMENT_ID", "505415")
LAB_NAME = os.environ.get("LAB_NAME", "lab3")
SKIP_TLS_ABUSE = os.environ.get("SKIP_TLS_ABUSE", "0") == "1"
TLS_FAILURES = int(os.environ.get("TLS_FAILURES", "2"))

# ---------------------------------------------------------------------------
# Load test student credentials
# ---------------------------------------------------------------------------

_students = []

def load_test_students():
    global _students
    if _students:
        return
    if not os.path.isfile(ROSTER_CSV):
        raise FileNotFoundError(
            f"Test roster not found: {ROSTER_CSV}\n"
            f"Run: python generate_test_students.py --output {ROSTER_CSV}"
        )
    with open(ROSTER_CSV, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            _students.append({
                "netid": row["netid"].strip(),
                "password": row["password"].strip(),
            })
    if not _students:
        raise ValueError(f"No students found in {ROSTER_CSV}")
    logging.info("Loaded %d test students from %s", len(_students), ROSTER_CSV)

# Assign students round-robin to Locust users
_student_iter = None

def next_student():
    global _student_iter
    if _student_iter is None:
        load_test_students()
        _student_iter = cycle(_students)
    return next(_student_iter)


# ---------------------------------------------------------------------------
# Dummy file content for uploads
# ---------------------------------------------------------------------------

def make_c_file(filename, student_netid):
    """Generate a minimal .c file that's different per student."""
    return (
        f"// Uploaded by {student_netid} during load test\n"
        f"// File: {filename}\n"
        f"#include <stdint.h>\n"
        f"// timestamp: {time.time()}\n"
    ).encode("utf-8")


def make_writeup(student_netid):
    """Generate a tiny writeup.txt."""
    return (
        f"Load test writeup for {student_netid}\n"
        f"This is a placeholder writeup for load testing.\n"
    ).encode("utf-8")


# ---------------------------------------------------------------------------
# TLS handshake abuse (simulates browsers rejecting self-signed cert)
# ---------------------------------------------------------------------------

def simulate_failed_tls_handshake(host, port):
    """
    Open a TCP connection, start a TLS handshake, then immediately close
    without completing it. This is what happens when a browser shows the
    "Your connection is not private" page — the TCP connection is established
    but the TLS handshake is abandoned.

    With sync gunicorn workers, each of these ties up a worker until timeout.
    """
    sock = None
    try:
        sock = socket.create_connection((host, port), timeout=3)
        # Send a ClientHello but with verify=True so it will fail on self-signed
        ctx = ssl.create_default_context()  # requires valid cert
        try:
            ctx.wrap_socket(sock, server_hostname=host)
        except ssl.SSLCertVerificationError:
            pass  # expected — this is the "browser rejecting cert" scenario
        except ssl.SSLError:
            pass
    except (ConnectionRefusedError, OSError):
        pass
    finally:
        if sock:
            try:
                sock.close()
            except Exception:
                pass


# ---------------------------------------------------------------------------
# Template files to upload (auto-discovered or hardcoded fallback)
# ---------------------------------------------------------------------------

def get_template_code_files():
    """
    Return list of .c/.h filenames for the lab. Tries to read from
    the actual template directory; falls back to a hardcoded list for Lab 3.
    """
    template_dir = os.path.join("template_files", LAB_NAME)
    if os.path.isdir(template_dir):
        files = sorted(
            f for f in os.listdir(template_dir)
            if f.endswith((".c", ".h")) and f != "lab.yaml"
        )
        if files:
            return files

    # Fallback for lab3
    if LAB_NAME == "lab3":
        return [
            "hw_interface.c",
            "hw_interface.h",
            "lab3.c",
            "startup_mspm0g350x_ticlang.c",
            "state_machine_logic.c",
            "state_machine_logic.h",
        ]
    # Fallback for lab1/lab2
    return [
        "delay.c",
        "delay.h",
        "initialize_leds.c",
        "initialize_leds.h",
        f"{LAB_NAME}.c",
        "startup_mspm0g350x_ticlang.c",
        "state_machine_logic.c",
        "state_machine_logic.h",
    ]


# ---------------------------------------------------------------------------
# Metrics tracking
# ---------------------------------------------------------------------------

@events.init.add_listener
def on_init(environment, **kwargs):
    """Log test configuration at startup."""
    logging.info("=" * 60)
    logging.info("DALI Load Test Configuration")
    logging.info("  Roster:        %s", ROSTER_CSV)
    logging.info("  Assignment ID: %s", ASSIGNMENT_ID)
    logging.info("  Lab:           %s", LAB_NAME)
    logging.info("  TLS abuse:     %s (%d per user)", "OFF" if SKIP_TLS_ABUSE else "ON", TLS_FAILURES)
    logging.info("  Code files:    %s", ", ".join(get_template_code_files()))
    logging.info("=" * 60)


# ---------------------------------------------------------------------------
# Locust User
# ---------------------------------------------------------------------------

class StudentUser(HttpUser):
    """
    Simulates a single student going through the full DALI workflow.

    Each user:
      1. Simulates failed TLS handshakes (optional)
      2. Logs in
      3. Loads the home page
      4. Navigates to the assignment
      5. Uploads each code file (one at a time, with small delays)
      6. Uploads a writeup
      7. Triggers compilation
      8. Polls compilation status until complete
      9. Waits, then starts over (simulating re-uploads / recompiles)
    """

    # Wait 10-30 seconds between full workflow cycles
    wait_time = between(10, 30)

    def on_start(self):
        """Called once when a simulated user starts."""
        self.student = next_student()
        self.netid = self.student["netid"]
        self.code_files = get_template_code_files()
        self.logged_in = False

        # Parse host for TLS abuse
        from urllib.parse import urlparse
        parsed = urlparse(self.host)
        self.target_host = parsed.hostname or "localhost"
        self.target_port = parsed.port or (443 if parsed.scheme == "https" else 5000)
        self.is_https = parsed.scheme == "https"

        # Step 1: Simulate failed TLS handshakes
        if self.is_https and not SKIP_TLS_ABUSE:
            for i in range(TLS_FAILURES):
                start = time.time()
                simulate_failed_tls_handshake(self.target_host, self.target_port)
                elapsed = time.time() - start
                # Report as a custom metric so it shows up in Locust stats
                events.request.fire(
                    request_type="TLS",
                    name="failed_handshake",
                    response_time=elapsed * 1000,
                    response_length=0,
                    exception=None,
                    context={},
                )
                # Small delay between retries (browser behavior)
                time.sleep(random.uniform(0.5, 2.0))

        # Step 2: Log in
        self._login()

    def _login(self):
        """Log in and verify success."""
        with self.client.post(
            "/login",
            data={"netid": self.netid, "password": self.student["password"]},
            catch_response=True,
            name="/login",
            allow_redirects=False,
        ) as resp:
            # Successful login returns a 302 redirect to /
            if resp.status_code in (200, 302):
                self.logged_in = True
                resp.success()
            else:
                resp.failure(f"Login failed: HTTP {resp.status_code}")

        # Follow the redirect to home
        if self.logged_in:
            self.client.get("/", name="/home")

    @task
    def full_workflow(self):
        """Execute the complete student workflow."""
        if not self.logged_in:
            self._login()
            if not self.logged_in:
                return

        # Step 3: Load assignment page
        with self.client.get(
            f"/assignment/{ASSIGNMENT_ID}",
            catch_response=True,
            name="/assignment/[id]",
        ) as resp:
            if resp.status_code != 200:
                resp.failure(f"Assignment page: HTTP {resp.status_code}")
                return

        # Small pause — student reads the page
        time.sleep(random.uniform(1, 3))

        # Step 4: Upload each code file
        for filename in self.code_files:
            content = make_c_file(filename, self.netid)
            files = {"file": (filename, io.BytesIO(content), "text/plain")}

            with self.client.post(
                f"/upload/{ASSIGNMENT_ID}/{filename}",
                files=files,
                catch_response=True,
                name="/upload/[id]/[filename]",
            ) as resp:
                if resp.status_code == 200:
                    try:
                        data = resp.json()
                        if data.get("success"):
                            resp.success()
                        else:
                            resp.failure(f"Upload {filename}: {data.get('error')}")
                    except Exception:
                        resp.failure(f"Upload {filename}: bad JSON")
                else:
                    resp.failure(f"Upload {filename}: HTTP {resp.status_code}")

            # Small delay between uploads (realistic typing/clicking)
            time.sleep(random.uniform(0.5, 2.0))

        # Step 5: Upload writeup
        writeup_content = make_writeup(self.netid)
        files = {"file": ("writeup.txt", io.BytesIO(writeup_content), "text/plain")}

        with self.client.post(
            f"/upload/{ASSIGNMENT_ID}/writeup.txt",
            files=files,
            catch_response=True,
            name="/upload/[id]/writeup.txt",
        ) as resp:
            if resp.status_code == 200:
                resp.success()
            else:
                resp.failure(f"Writeup upload: HTTP {resp.status_code}")

        time.sleep(random.uniform(1, 2))

        # Step 6: Reload assignment page to see upload results
        with self.client.get(
            f"/assignment/{ASSIGNMENT_ID}",
            catch_response=True,
            name="/assignment/[id] (after upload)",
        ) as resp:
            if resp.status_code == 200:
                resp.success()
            else:
                resp.failure(f"Assignment reload: HTTP {resp.status_code}")

        time.sleep(random.uniform(1, 3))

        # Step 7: Trigger compilation
        job_id = None
        with self.client.post(
            f"/compile/{ASSIGNMENT_ID}",
            catch_response=True,
            name="/compile/[id]",
        ) as resp:
            if resp.status_code == 200:
                try:
                    data = resp.json()
                    if data.get("success"):
                        job_id = data["job_id"]
                        resp.success()
                    else:
                        resp.failure(f"Compile start: {data.get('error')}")
                except Exception:
                    resp.failure("Compile start: bad JSON")
            else:
                resp.failure(f"Compile start: HTTP {resp.status_code}")

        if not job_id:
            return

        # Step 8: Poll compilation status until complete
        compile_start = time.time()
        max_poll_time = 120  # give up after 2 minutes
        poll_count = 0
        final_status = None

        while time.time() - compile_start < max_poll_time:
            time.sleep(1)
            poll_count += 1

            with self.client.get(
                f"/compile-status/{job_id}",
                catch_response=True,
                name="/compile-status/[job_id]",
            ) as resp:
                if resp.status_code != 200:
                    resp.failure(f"Poll: HTTP {resp.status_code}")
                    continue

                try:
                    status = resp.json()
                    resp.success()
                except Exception:
                    resp.failure("Poll: bad JSON")
                    continue

                state = status.get("status")
                if state in ("complete", "failed"):
                    final_status = state
                    break
                elif state == "cancelled":
                    final_status = "cancelled"
                    break

        # Report the full compile cycle as a custom metric
        compile_elapsed = time.time() - compile_start
        events.request.fire(
            request_type="COMPILE",
            name=f"full_compile_cycle ({final_status or 'timeout'})",
            response_time=compile_elapsed * 1000,
            response_length=0,
            exception=None if final_status else TimeoutError("Compile timed out"),
            context={},
        )

        # Also track poll count
        events.request.fire(
            request_type="COMPILE",
            name="poll_count",
            response_time=poll_count,  # abuse response_time to track count
            response_length=0,
            exception=None,
            context={},
        )

        # Step 9: Reload assignment page to see compile results
        if final_status:
            with self.client.get(
                f"/assignment/{ASSIGNMENT_ID}",
                catch_response=True,
                name="/assignment/[id] (after compile)",
            ) as resp:
                if resp.status_code == 200:
                    resp.success()
                else:
                    resp.failure(f"Final reload: HTTP {resp.status_code}")
