# DALI — Dynamic Assignment Lab Interface

> Surreally simple submissions for embedded systems

DALI is a lab submission system for embedded systems courses. Students upload code files, test compilation against the TI ARM toolchain, and submit to Canvas — with a consistent file structure every time.

Built for **ELEC 327** at Rice University, targeting the TI MSPM0G3507.

---

## Features

**Students** upload only the files you specify, test compilation before submitting, see their queue position in real time, and submit a clean zip to Canvas with one click. Template files fill in anything they don't upload.

**Instructors** get a live admin dashboard showing the compilation queue, per-assignment lab configs that enforce exactly which files are expected, and submissions that always have the same archive structure.

---

## Architecture

```
Flask web app  ──►  Redis queue  ──►  Worker process(es)
(app_complete.py)                     (compile_worker_main.py)
                                           │
                                      TI ARM Clang
                                      (tiarmclang)
```

The web app enqueues compilation jobs. A separate worker process picks them up, generates a Makefile, runs `make`, and reports results back through Redis. You need both processes running.

---

## Project Structure

```
dali/
├── app_complete.py              # Flask application
├── compile_queue.py             # Redis job queue + worker logic
├── compile_worker_main.py       # Standalone worker process
├── makefile_generator.py        # Generates Makefiles for TI toolchain
├── student_passwords.csv        # Student roster (netid, name, canvas_id, password)
├── templates/                   # HTML templates
│   ├── login_api.html
│   ├── home_api.html
│   ├── assignment_api.html
│   ├── view_file.html
│   ├── admin_queue.html
│   └── admin_login.html
├── template_files/              # Lab templates (auto-discovered)
│   └── lab3/
│       ├── lab.yaml             # Lab config (display name, assignment ID, writeup files)
│       ├── hw_interface.c       # Auto-discovered as code file
│       ├── hw_interface.h
│       ├── lab3.c
│       ├── startup_mspm0g350x_ticlang.c
│       ├── state_machine_logic.c
│       ├── state_machine_logic.h
│       └── mspm0g3507.cmd       # Auto-copied as build infrastructure
└── uploads/                     # Student submissions (auto-created)
    └── student_{canvas_id}/
        └── assignment_{id}/
```

---

## Setup

### Prerequisites

- Python 3.8+
- Redis
- TI ARM Clang compiler + MSPM0 SDK
- Canvas API token with course access

### Install

```bash
git clone https://github.com/ckemere/dali.git
cd dali
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

### Environment Variables

All configuration is via environment variables. Create a `.env` file or export them directly:

```bash
# Required
export FLASK_SECRET_KEY="$(python3 -c 'import secrets; print(secrets.token_hex(32))')"
export CANVAS_API_TOKEN="your_canvas_api_token"
export COURSE_ID="your_canvas_course_id"
export ADMIN_PASSWORD="$(python3 -c 'import secrets; print(secrets.token_urlsafe(16))')"

# Canvas
export CANVAS_BASE_URL="https://canvas.rice.edu"      # default

# Student roster
export ROSTER_CSV_PATH="student_passwords.csv"         # default

# Redis
export REDIS_HOST="localhost"                           # default
export REDIS_PORT="6379"                                # default

# TI ARM Clang toolchain
export TI_COMPILER_ROOT="/home/elec327/ti/ccs2041/ccs/tools/compiler/ti-cgt-armllvm_4.0.4.LTS"
export TI_SDK_ROOT="/home/elec327/ti/mspm0_sdk_2_09_00_01"
export PATH="$TI_COMPILER_ROOT/bin:$PATH"

# Worker tuning (optional)
export COMPILE_WORKERS="8"                              # default: 8
export COMPILE_MAX_RUNTIME="60"                         # seconds, default: 60
export COMPILE_STALE_SECONDS="30"                       # heartbeat timeout, default: 30
```

### Student Roster

Create `student_passwords.csv` with one row per student:

```csv
netid,name,canvas_id,password
ts1000,"Student, Test",106586,X_ODy9#ZCOnP
jd2000,"Doe, Jane",108842,kR7$mPqW2xNv
```

Students log in with their NetID and the password you assign. After changing the roster or lab configs, reload gunicorn: `kill -HUP $(cat gunicorn.pid)` or restart it.

### Run

```bash
# Terminal 1: Redis
redis-server

# Terminal 2: Web app
python3 app_complete.py

# Terminal 3: Compile workers
python3 compile_worker_main.py

# Visit http://localhost:5000
# Admin dashboard at /admin/compile-queue
```

---

## Lab Configuration

Labs are auto-discovered from `template_files/`. Each lab is a subdirectory containing a `lab.yaml` file and the template source files.

**To add a new lab:**

1. Create a directory: `mkdir template_files/lab4`
2. Drop in all source files (`.c`, `.h`) and the linker script (`.cmd`)
3. Create `lab.yaml`:

```yaml
# template_files/lab4/lab.yaml
display_name: "Lab 4"
canvas_assignment_id: "507123"
writeup_files:
  - writeup.txt
  - writeup.pdf
```

That's it. On startup (or via `POST /admin/reload-labs`), DALI scans `template_files/*/lab.yaml` and auto-discovers:

- **Code files**: all `.c` and `.h` files in the directory — shown to students as uploadable/editable
- **Infrastructure files**: everything else (`.cmd`, etc.) — copied into builds automatically, not shown to students
- **Writeup files**: listed in the YAML since they don't exist in the template directory

The `canvas_assignment_id` is the numeric ID from the Canvas assignment URL (`/courses/.../assignments/XXXXXX`). Only assignments with a matching lab config appear on the student home page.

Students can also upload additional `.c`/`.h` files not in the template, or exclude template files they don't need.

---

## How Submission Works

When a student clicks "Submit to Canvas":

1. A zip is built from: template files (minus any the student excluded) + student uploads + any extra files the student added + writeup
2. The zip is uploaded to Canvas as a **submission comment attachment** on the assignment
3. The comment includes a timestamp

At compile time, the same merge happens into a temp directory, a Makefile is generated from all `.c` files present, and `make` runs with the TI toolchain flags.

---

## Endpoints

| Endpoint | Description |
|---|---|
| `/login` | Student login (NetID + password) |
| `/` | Assignment list (filtered to configured labs) |
| `/assignment/<id>` | Upload files, compile, submit |
| `/upload/<id>/<filename>` | Upload/replace a file (POST) |
| `/upload-extra/<id>` | Add a new .c/.h file (POST) |
| `/exclude/<id>/<filename>` | Exclude a template file from build (POST) |
| `/restore/<id>/<filename>` | Restore an excluded file (POST) |
| `/delete-extra/<id>/<filename>` | Delete a student-added file (POST) |
| `/compile/<id>` | Start compilation (POST) |
| `/compile-status/<job_id>` | Poll compilation status |
| `/compile-cancel/<job_id>` | Cancel queued job (POST) |
| `/submit/<id>` | Submit to Canvas (POST) |
| `/admin/compile-queue` | Admin dashboard |
| `/health` | Health check (Redis, queue, roster, labs) |

---

## HTTPS

For production, put nginx in front as a reverse proxy with TLS:

```nginx
server {
    listen 443 ssl;
    server_name dali.rice.edu;
    ssl_certificate     /path/to/fullchain.pem;
    ssl_certificate_key /path/to/privkey.pem;

    location / {
        proxy_pass http://127.0.0.1:5000;
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-Proto $scheme;
    }
}
```

---

## Troubleshooting

**Jobs stuck in "queued"** — The worker process isn't running. Start `compile_worker_main.py` in a separate terminal.

**404 on admin dashboard** — The URL is `/admin/compile-queue` (hyphen, not underscore).

**"tiarmclang: command not found"** — `TI_COMPILER_ROOT` is wrong or not on `PATH`.

**Redis connection refused** — Start Redis: `sudo systemctl start redis-server`

**Check overall health** — Hit `/health` for Redis status, queue depth, and roster count.

---

## License

GPLv3. See [LICENSE](LICENSE).

## Contact

Caleb Kemere — Rice University
[GitHub Issues](https://github.com/ckemere/dali/issues)
