<p align="center">
  <img src="Logo.png" alt="DALI Logo" width="300">
</p>

# DALI — Dynamic Assignment Lab Interface

> Surreally simple submissions for embedded systems

DALI is a lab submission system for embedded systems courses. Students upload code files, test compilation against the TI ARM toolchain, and submit to Canvas — with a consistent file structure every time.

Built for **ELEC 327** at Rice University, targeting the TI MSPM0G3507.

---

## Features

**Students** upload only the files you specify, test compilation before submitting, see their queue position in real time, and submit a clean zip to Canvas with one click. Template files fill in anything they don't upload.

**Instructors** get a live admin dashboard showing the compilation queue, per-assignment lab configs that enforce exactly which files are expected, and submissions that always have the same archive structure. Submissions appear directly in SpeedGrader as the student's actual submission.

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
- The Canvas account behind the API token must have "Become other users" (a.k.a. "Users - act as") permission — this is required for uploading submission files on behalf of students

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

# Submission mode (optional)
export SUBMIT_AS_UPLOAD="true"                          # default: true (actual Canvas submission)
                                                        # set to "false" for legacy comment-attachment mode

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

**Important:** For the default submission mode (`SUBMIT_AS_UPLOAD=true`), each Canvas assignment must have `Online - File Uploads` enabled in its submission type settings. If file uploads are not an allowed submission type, Canvas will reject the submission with a 400 error.

---

## How Submission Works

When a student clicks "Submit to Canvas":

1. A zip is built from: template files (minus any the student excluded) + student uploads + any extra files the student added + writeup
2. The zip is uploaded to Canvas as the student's **actual submission** using the `online_upload` submission type, via Canvas API masquerading (`as_user_id`)
3. A submission comment with a timestamp is also attached for audit trail
4. If scoring is configured, a grade is posted automatically

The submission appears directly in SpeedGrader as the student's submission — instructors can download the zip, view the submission history, and grade it just like any other Canvas file upload submission.

**Legacy mode:** Set `SUBMIT_AS_UPLOAD=false` to instead upload the zip as a submission comment attachment (the original behavior). In this mode the zip does not appear as a formal submission in SpeedGrader — only as a comment attachment.

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
| `/health` | Health check (Redis, queue, roster, labs, submit mode) |

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

**403 "user not authorized" on submission** — Your Canvas API token's account needs the "Become other users" permission. This is required for masquerading as students during file upload. Contact your Canvas admin to enable "Users - act as" for your role.

**400 "Invalid submission type" on submission** — The Canvas assignment doesn't have `Online - File Uploads` enabled. Edit the assignment settings in Canvas and add it as an allowed submission type.

**Want to use the old comment-attachment mode?** — Set `SUBMIT_AS_UPLOAD=false` in your environment. The zip will be attached to a submission comment instead of being submitted as a formal submission.

**Check overall health** — Hit `/health` for Redis status, queue depth, roster count, and current submission mode.

---

## License

GPLv3. See [LICENSE](LICENSE).

## Contact

Caleb Kemere — Rice University
[GitHub Issues](https://github.com/ckemere/dali/issues)
