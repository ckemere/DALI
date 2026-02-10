# DALI Load Testing

Load tests for the DALI submission system using [Locust](https://locust.io/).

## Setup

```bash
# Create a virtual environment (from the DALI project root)
python3 -m venv test-venv
source test-venv/bin/activate
pip install locust

# Generate 100 fake test students
python3 testing/generate_test_students.py --output testing/test_students.csv
```

Start DALI with the test roster:

```bash
ROSTER_CSV_PATH=testing/test_students.csv gunicorn app_complete:app \
    --certfile cert.pem --keyfile key.pem \
    --workers 4 --worker-class gevent \
    --worker-connections 100 \
    --timeout 120 \
    --bind 0.0.0.0:5000
```

Make sure the compile worker is running in a separate terminal:

```bash
python3 compile_worker_main.py
```

## Running Tests

All commands should be run from the DALI project root so the locustfile can find `template_files/`.

### Quick smoke test (5 users)

```bash
SKIP_TLS_ABUSE=1 TEST_ROSTER_CSV=testing/test_students.csv \
    locust -f testing/locustfile.py --host https://granule.rice.edu:5000 \
    --users 5 --spawn-rate 1 --run-time 1m --headless
```

### Classroom simulation (50 users)

```bash
SKIP_TLS_ABUSE=1 TEST_ROSTER_CSV=testing/test_students.csv \
    locust -f testing/locustfile.py --host https://granule.rice.edu:5000 \
    --users 50 --spawn-rate 5 --run-time 5m --headless \
    --html testing/results/classroom.html
```

### Full stress test (100 users)

```bash
SKIP_TLS_ABUSE=1 TEST_ROSTER_CSV=testing/test_students.csv \
    locust -f testing/locustfile.py --host https://granule.rice.edu:5000 \
    --users 100 --spawn-rate 10 --run-time 10m --headless \
    --html testing/results/stress.html
```

### With TLS failure simulation

To reproduce the self-signed certificate issue from the first in-class test, drop `SKIP_TLS_ABUSE` and optionally increase the number of failed handshakes per user:

```bash
TLS_FAILURES=4 TEST_ROSTER_CSV=testing/test_students.csv \
    locust -f testing/locustfile.py --host https://granule.rice.edu:5000 \
    --users 50 --spawn-rate 5 --run-time 5m --headless
```

### Interactive mode

Opens a web UI at `http://localhost:8089` where you can adjust users in real time and watch response time graphs:

```bash
TEST_ROSTER_CSV=testing/test_students.csv \
    locust -f testing/locustfile.py --host https://granule.rice.edu:5000
```

## What Each User Does

Each simulated student follows this workflow:

1. Simulates failed TLS handshakes (skippable with `SKIP_TLS_ABUSE=1`)
2. Logs in with their test credentials
3. Loads the assignment page
4. Uploads each template code file (real files from `template_files/`)
5. Uploads a writeup
6. Reloads the assignment page (to see upload results)
7. Triggers compilation
8. Polls until compilation finishes
9. Reloads the assignment page (to see compile results)
10. Waits 10–30 seconds, then repeats

## Reading the Results

Key rows in the Locust stats output:

| Row | What it tells you |
|---|---|
| `/login` | How long students wait to log in |
| `/assignment/[id]` | Page load time |
| `/upload/[id]/[filename]` | File upload speed |
| `full_compile_cycle (complete)` | Total time from clicking compile to seeing the result |
| `full_compile_cycle (timeout)` | Compilations that took over 2 minutes (bad) |
| `poll_count` | How many status polls before the result came back |
| `failed_handshake` | TLS abuse simulation timing |

Things to watch for:

- **`full_compile_cycle` avg climbing** — compile workers are backed up
- **`poll_count` increasing** — jobs are sitting in the queue longer
- **`/login` or `/assignment` times above 2s** — gunicorn workers are saturated
- **Failure rate increasing** — server is overloaded

## Environment Variables

| Variable | Default | Description |
|---|---|---|
| `TEST_ROSTER_CSV` | `test_students.csv` | Path to the test student roster |
| `ASSIGNMENT_ID` | `505415` | Canvas assignment ID to test against |
| `LAB_NAME` | `lab3` | Lab template directory name |
| `SKIP_TLS_ABUSE` | `0` | Set to `1` to skip failed TLS handshakes |
| `TLS_FAILURES` | `2` | Number of failed handshakes per user |

## Files

| File | Purpose |
|---|---|
| `testing/locustfile.py` | Locust load test definition |
| `testing/generate_test_students.py` | Generates fake student roster |
| `testing/run_load_test.sh` | Convenience wrapper with preset scenarios |
| `testing/test_students.csv` | Generated test roster (not committed) |
| `testing/results/` | HTML reports from test runs (not committed) |
