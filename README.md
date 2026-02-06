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
├── template_files/              # Lab template source files
│   └── lab3/
│       ├── hw_interface.c
│       ├── hw_interface.h
│       ├── lab3.c
│       ├── startup_mspm0g350x_ticlang.c
│       ├── state_machine_logic.c
│       ├── state_machine_logic.h
│       └── mspm0g3507.cmd
└── uploads/                     # Student submissions (auto-created)
    └── student_{canvas_id}/
        └── assignment_{assignment_id}/
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

Students log in with their NetID and the password you assign. The roster can be reloaded without restarting via `POST /admin/reload-roster`.

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

Labs are defined in `LAB_CONFIGS` in `app_complete.py`, keyed by **Canvas assignment ID**:

```python
LAB_CONFIGS = {
    "505415": {                          # Canvas assignment ID (string)
        "display_name": "Lab 3",
        "template_dir": "lab3",          # subdirectory under template_files/
        "code_files": [
            "hw_interface.c",
            "hw_interface.h",
            "lab3.c",
            "startup_mspm0g350x_ticlang.c",
            "state_machine_logic.c",
            "state_machine_logic.h",
        ],
        "writeup_files": ["writeup.txt", "writeup.pdf"],
    },
}
```

**To add a new lab:**

1. Find the Canvas assignment ID (from the URL: `/courses/.../assignments/XXXXXX`)
2. Add an entry to `LAB_CONFIGS` with that ID as the key
3. Create `template_files/<template_dir>/` with all source files and the linker script (`.cmd`)
4. List every file students might modify in `code_files`
5. Students see template defaults for any file they haven't uploaded

---

## How Submission Works

When a student clicks "Submit to Canvas":

1. A zip is built merging template files with any student uploads (student files override templates)
2. The zip is uploaded to Canvas as a **submission comment attachment** on the assignment
3. The comment includes a timestamp

At compile time, the same merge happens into a temp directory, a Makefile is generated, and `make` runs with the TI toolchain flags.

---

## Endpoints

| Endpoint | Description |
|---|---|
| `/login` | Student login (NetID + password) |
| `/` | Assignment list |
| `/assignment/<id>` | Upload files, compile, submit |
| `/compile/<id>` | Start compilation (POST) |
| `/compile-status/<job_id>` | Poll compilation status |
| `/compile-cancel/<job_id>` | Cancel queued job (POST) |
| `/submit/<id>` | Submit to Canvas (POST) |
| `/admin/compile-queue` | Admin dashboard |
| `/admin/reload-roster` | Reload CSV without restart (POST) |
| `/health` | Health check (Redis status, queue depth, roster count) |

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
