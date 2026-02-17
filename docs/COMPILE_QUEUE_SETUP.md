# Compilation Queue Setup

## Overview

The compilation queue provides:
- **Queue position tracking** — students see "You are #5 in queue"
- **NetID display** — admin dashboard shows Rice netIDs, not Canvas IDs
- **Job cancellation** — students can cancel queued jobs
- **Admin dashboard** — real-time view of all compilation jobs at `/admin/compile-queue`
- **Multi-core compilation** — parallel processing (default: 8 workers, tunable)

## Architecture

```
Student clicks "Test Compilation"
    ↓
Job added to Redis queue
    ↓
compile_worker_main.py picks up job
    ↓
Compiles in a temporary build directory
    ↓
Result stored in Redis + written to student folder
    ↓
Student sees results
```

No Docker needed — direct `tiarmclang` invocation via `makefile_generator.py`.

## Installation

### 1. Install Redis

```bash
# Ubuntu/Debian
sudo apt update
sudo apt install redis-server
sudo systemctl enable redis
sudo systemctl start redis

# macOS
brew install redis
brew services start redis
```

### 2. Install Python Dependencies

```bash
pip install -r requirements.txt
```

Redis is already listed in `requirements.txt`. If adding manually:
```
redis==4.5.4
```

### 3. Install the TI ARM Clang Toolchain

See `docs/TI_TOOLCHAIN_SETUP.md` for full instructions. The short version:

```bash
export TI_COMPILER_ROOT="/path/to/ti-cgt-armllvm_4.0.4.LTS"
export TI_SDK_ROOT="/path/to/mspm0_sdk_2_09_00_01"
export PATH="$TI_COMPILER_ROOT/bin:$PATH"
```

Verify:
```bash
which tiarmclang
python3 -c "from makefile_generator import verify_toolchain; print(verify_toolchain())"
# (True, 'Toolchain verified successfully')
```

### 4. Configure Environment Variables

Add to your `.env` (or export directly):

```bash
# Redis
export REDIS_HOST="localhost"       # default
export REDIS_PORT="6379"            # default

# Admin dashboard password
export ADMIN_PASSWORD="your_secure_password_here"

# Worker tuning (optional)
export COMPILE_WORKERS="8"          # default: 8; set to match core count
export COMPILE_MAX_RUNTIME="60"     # seconds per job, default: 60
export COMPILE_STALE_SECONDS="30"   # heartbeat timeout, default: 30
```

The roster-to-netID mapping is loaded automatically from `ROSTER_CSV_PATH`
(default: `student_passwords.csv`) — no separate gradebook export needed.

### 5. Add the Linker Script to Each Lab Template

Each lab directory needs the TI `.cmd` linker script. See
`docs/TI_TOOLCHAIN_SETUP.md → "Add Linker Script to Templates"` for how to
extract it from CCS.

```bash
cp mspm0g3507.cmd template_files/lab3/
# repeat for each lab
```

### 6. Run the System

Three processes must be running:

```bash
# Terminal 1: Redis (if not running as a service)
redis-server

# Terminal 2: Flask web app
python3 app_complete.py
# or with gunicorn:
gunicorn app_complete:app --workers 4 --worker-class gevent \
    --worker-connections 100 --timeout 120 --bind 0.0.0.0:5000

# Terminal 3: Compile worker(s)
python3 compile_worker_main.py
```

### 7. Test the System

**As a student:**
1. Go to `http://localhost:5000`
2. Log in, select an assignment
3. Click "🔨 Test Compilation"
4. See queue position: "Position in queue: #1"
5. Wait for compilation, see results

**As admin:**
1. Go to `http://localhost:5000/admin/compile-queue`
2. Enter admin password
3. See all active jobs with netIDs and timing

---

## Student Workflow

1. Log in → select assignment → upload files
2. Click **"🔨 Test Compilation"**
3. Status updates in real time:
   - `⏳ In Queue — Position #3, estimated wait: 8 seconds`
   - `⚙️ Compiling...`
   - `✅ Compilation Successful!` or `❌ Compilation Failed` with error output
4. Fix errors, re-upload, compile again
5. When green, click **"Submit to Canvas"**

Students can cancel a queued job (not one already compiling) at any time.

---

## Admin Dashboard

Access: `http://yourserver/admin/compile-queue`

Shows a live table of all queued and active jobs:

```
Pos │ NetID │ Student Name │ Assignment │ Status       │ Duration
────┼───────┼──────────────┼────────────┼──────────────┼─────────
 #1 │ jd123 │ John Doe     │ Lab 3      │ ⏳ Queued    │ 3s
 #2 │ js456 │ Jane Smith   │ Lab 3      │ ⏳ Queued    │ 2s
  — │ ba789 │ Bob Andrews  │ Lab 3      │ ⚙️ Compiling │ 7s
```

Auto-refreshes every 2 seconds. Useful during deadline rushes to monitor
queue depth and spot students having repeated failures.

---

## Configuration Options

### Worker count

Set via environment variable (preferred):
```bash
export COMPILE_WORKERS=16
```

Or pass directly when instantiating `CompilationQueue` in `compile_queue.py`:
```python
compile_queue = CompilationQueue(max_workers=16)
```

Match worker count to available cores. The default of 8 is conservative.

### Compilation timeout

Set via environment variable:
```bash
export COMPILE_MAX_RUNTIME=90   # seconds
```

### Poll interval (student UI)

In `templates/assignment_api.html`, the JavaScript polls every second by default.
Adjust `setInterval(checkStatus, 1000)` if needed.

---

## Troubleshooting

**"Connection refused" on compile submit**
→ Redis is not running. `sudo systemctl start redis`

**"tiarmclang: command not found" in worker logs**
→ `TI_COMPILER_ROOT` not set or not on `PATH`. Check `.env` and worker process environment.

**NetIDs showing as blank or "unknown"**
→ The netID comes from `student_passwords.csv` (via `ROSTER_CSV_PATH`). Verify the
   CSV has a `netid` column and the path is correct.

**Jobs stuck in queue, never compiling**
→ The worker process crashed or was never started. Check `compile_worker_main.py`
   is running. Workers restart jobs automatically on startup.

**Compilation timeout**
→ Increase `COMPILE_MAX_RUNTIME`. Also check for missing `.cmd` linker script
   in the lab template directory — a missing linker script causes `make` to hang.
