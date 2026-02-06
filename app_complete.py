import os
import io
import json
import zipfile
import shutil
import logging
from datetime import datetime
from flask import (
    Flask, request, render_template, jsonify, session,
    redirect, url_for, flash, send_file,
)
from werkzeug.utils import secure_filename
import requests

import csv
import hmac

from compile_queue import CompilationQueue

# =============================================================================
# ENVIRONMENT / CONFIG VALIDATION
# =============================================================================

def require_env(name):
    value = os.environ.get(name)
    if not value:
        raise RuntimeError(f"Required environment variable {name} is not set")
    return value

FLASK_SECRET_KEY = require_env("FLASK_SECRET_KEY")
CANVAS_API_TOKEN = require_env("CANVAS_API_TOKEN")
COURSE_ID = require_env("COURSE_ID")
ADMIN_PASSWORD = require_env("ADMIN_PASSWORD")

CANVAS_BASE_URL = os.environ.get("CANVAS_BASE_URL", "https://canvas.rice.edu")
REDIS_HOST = os.environ.get("REDIS_HOST", "localhost")
REDIS_PORT = int(os.environ.get("REDIS_PORT", "6379"))
GRADEBOOK_CSV_PATH = os.environ.get("GRADEBOOK_CSV_PATH", "gradebook.csv")

UPLOAD_FOLDER = "uploads"
TEMPLATE_FOLDER = "template_files"
ALLOWED_CODE_EXTENSIONS = {"c", "h"}
ALLOWED_DOC_EXTENSIONS = {"txt", "pdf"}

os.makedirs(UPLOAD_FOLDER, exist_ok=True)
os.makedirs(TEMPLATE_FOLDER, exist_ok=True)

# =============================================================================
# STUDENT ROSTER (loaded from CSV)
# =============================================================================

# Keyed by netid → { "netid", "name", "canvas_id", "password" }
STUDENT_ROSTER = {}

def load_roster(csv_path):
    """Load student roster from CSV. Called at startup."""
    global STUDENT_ROSTER
    STUDENT_ROSTER = {}

    if not os.path.isfile(csv_path):
        logging.warning("Roster CSV not found at %s — no students can log in!", csv_path)
        return 0

    with open(csv_path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            netid = row["netid"].strip().lower()
            STUDENT_ROSTER[netid] = {
                "netid": netid,
                "name": row["name"].strip(),
                "canvas_id": row["canvas_id"].strip(),
                "password": row["password"].strip(),
            }

    logging.info("Loaded %d students from %s", len(STUDENT_ROSTER), csv_path)
    return len(STUDENT_ROSTER)

load_roster(GRADEBOOK_CSV_PATH)


def authenticate_student(netid, password):
    """
    Validate netid + password against the roster.
    Returns the student dict on success, None on failure.
    Uses constant-time comparison to prevent timing attacks.
    """
    netid = netid.strip().lower()
    student = STUDENT_ROSTER.get(netid)
    if not student:
        # Still do a comparison to keep timing constant
        hmac.compare_digest(password, "dummy_password_placeholder")
        return None

    if hmac.compare_digest(password, student["password"]):
        return student
    return None

# =============================================================================
# FLASK APP
# =============================================================================

app = Flask(__name__)
app.secret_key = FLASK_SECRET_KEY
app.config["UPLOAD_FOLDER"] = UPLOAD_FOLDER
app.config["MAX_CONTENT_LENGTH"] = 16 * 1024 * 1024  # 16 MB

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s: %(message)s",
    handlers=[logging.FileHandler("app.log"), logging.StreamHandler()],
)

# =============================================================================
# LAB CONFIGURATION
# =============================================================================

LAB_CONFIGS = {
    "505415": {   # Canvas assignment ID
        "display_name": "Lab 3",
        "template_dir": "lab3",
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

# =============================================================================
# QUEUE CLIENT (NO WORKERS HERE)
# =============================================================================

compile_queue = CompilationQueue(
    redis_host=REDIS_HOST,
    redis_port=REDIS_PORT,
)

# =============================================================================
# HELPERS
# =============================================================================

def allowed_file(filename, exts):
    return "." in filename and filename.rsplit(".", 1)[1].lower() in exts

def get_submission_folder(student_id, assignment_id):
    """Return (and create) the per-student, per-assignment upload directory."""
    path = os.path.join(UPLOAD_FOLDER, f"student_{student_id}", f"assignment_{assignment_id}")
    os.makedirs(path, exist_ok=True)
    return path

def get_template_file_path(lab_template_dir, filename):
    """Absolute path to a template file for a given lab."""
    return os.path.join(TEMPLATE_FOLDER, lab_template_dir, filename)

def get_lab_config_by_assignment_id(assignment_id):
    return LAB_CONFIGS.get(str(assignment_id))

def build_uploaded_files_status(student_folder, lab_config):
    """
    Build the status dict the assignment_api.html template expects.

    For every code and writeup file in the lab config, return:
        filename -> {
            "uploaded": bool,   # True if the student has uploaded their own version
            "size":     int,    # file size in bytes (0 if not uploaded)
            "modified": str,    # human-readable mtime (empty if not uploaded)
        }

    A file is considered "uploaded" only if it exists in the student's
    submission folder.  Template originals are never copied there; they
    are used as read-only fallbacks at compile / submit time.
    """
    all_files = lab_config["code_files"] + lab_config.get("writeup_files", [])
    status = {}
    for fname in all_files:
        fpath = os.path.join(student_folder, fname)
        if os.path.isfile(fpath):
            stat = os.stat(fpath)
            status[fname] = {
                "uploaded": True,
                "size": stat.st_size,
                "modified": datetime.fromtimestamp(stat.st_mtime).strftime(
                    "%Y-%m-%d %H:%M"
                ),
            }
        else:
            status[fname] = {
                "uploaded": False,
                "size": 0,
                "modified": "",
            }
    return status


def prepare_build_directory(student_folder, lab_config):
    """
    Create a temporary build directory that merges template files with
    student uploads.  Student files take priority over templates.

    Returns the path to the build directory.
    """
    import tempfile

    build_dir = tempfile.mkdtemp(prefix="dali_build_")
    template_dir = lab_config["template_dir"]

    for fname in lab_config["code_files"]:
        student_path = os.path.join(student_folder, fname)
        template_path = get_template_file_path(template_dir, fname)

        if os.path.isfile(student_path):
            shutil.copy2(student_path, os.path.join(build_dir, fname))
        elif os.path.isfile(template_path):
            shutil.copy2(template_path, os.path.join(build_dir, fname))
        else:
            logging.warning("File %s missing from both student dir and templates", fname)

    # Also copy the linker script and any other non-.c/.h files from template
    template_full_dir = os.path.join(TEMPLATE_FOLDER, template_dir)
    for fname in os.listdir(template_full_dir):
        dest = os.path.join(build_dir, fname)
        if not os.path.exists(dest):
            shutil.copy2(os.path.join(template_full_dir, fname), dest)

    return build_dir


def create_submission_zip(student_folder, lab_config):
    """
    Build an in-memory zip archive that merges template defaults with
    student uploads, plus the writeup.  Returns a BytesIO object.
    """
    buf = io.BytesIO()
    template_dir = lab_config["template_dir"]

    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        # Code files (student upload wins over template)
        for fname in lab_config["code_files"]:
            student_path = os.path.join(student_folder, fname)
            template_path = get_template_file_path(template_dir, fname)

            if os.path.isfile(student_path):
                zf.write(student_path, fname)
            elif os.path.isfile(template_path):
                zf.write(template_path, fname)

        # Writeup (student must provide this themselves)
        for fname in lab_config.get("writeup_files", []):
            student_path = os.path.join(student_folder, fname)
            if os.path.isfile(student_path):
                zf.write(student_path, fname)

    buf.seek(0)
    return buf

# =============================================================================
# CANVAS API
# =============================================================================

def canvas_api_request(endpoint, method="GET", data=None, files=None):
    url = f"{CANVAS_BASE_URL}/api/v1/{endpoint}"
    headers = {"Authorization": f"Bearer {CANVAS_API_TOKEN}"}

    if method == "GET":
        r = requests.get(url, headers=headers, timeout=30)
    elif method == "POST":
        if files:
            # multipart upload — don't set Content-Type, let requests handle it
            r = requests.post(url, headers=headers, data=data, files=files, timeout=60)
        else:
            r = requests.post(url, headers=headers, json=data, timeout=30)
    elif method == "PUT":
        r = requests.put(url, headers=headers, json=data, timeout=30)
    else:
        raise ValueError("Unsupported method")

    r.raise_for_status()
    return r.json()

# =============================================================================
# ROUTES – AUTH
# =============================================================================

@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        netid = request.form.get("netid", "")
        password = request.form.get("password", "")

        student = authenticate_student(netid, password)
        if student:
            session["student_id"] = student["canvas_id"]
            session["student_name"] = student["name"]
            session["netid"] = student["netid"]
            logging.info("Login: %s (%s)", student["netid"], student["name"])
            return redirect(url_for("home"))

        flash("Invalid NetID or password.")
    return render_template("login_api.html")

@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))

# =============================================================================
# ROUTES – MAIN
# =============================================================================

@app.route("/")
def home():
    if "student_id" not in session:
        return redirect(url_for("login"))
    assignments = canvas_api_request(f"courses/{COURSE_ID}/assignments")
    return render_template(
        "home_api.html",
        assignments=assignments,
        student_name=session["student_name"],
    )

@app.route("/assignment/<assignment_id>")
def assignment(assignment_id):
    if "student_id" not in session:
        return redirect(url_for("login"))

    assignment_data = canvas_api_request(
        f"courses/{COURSE_ID}/assignments/{assignment_id}"
    )
    lab = get_lab_config_by_assignment_id(assignment_id)

    if not lab:
        flash("No lab configuration found for this assignment.")
        return redirect(url_for("home"))

    student_folder = get_submission_folder(session["student_id"], assignment_id)
    uploaded_files = build_uploaded_files_status(student_folder, lab)

    return render_template(
        "assignment_api.html",
        # Variables the template actually uses:
        assignment_id=assignment_id,
        assignment_title=assignment_data.get("name", "Assignment"),
        student_name=session["student_name"],
        lab_name=lab["template_dir"],
        code_files=lab["code_files"],
        writeup_files=lab.get("writeup_files", []),
        uploaded_files=uploaded_files,
        compile_available=compile_queue.is_available(),
    )

# =============================================================================
# ROUTES – FILE MANAGEMENT
# =============================================================================

@app.route("/upload/<assignment_id>/<filename>", methods=["POST"])
def upload_file(assignment_id, filename):
    """Accept a single file upload from the student."""
    if "student_id" not in session:
        return jsonify(error="Not authenticated"), 403

    lab = get_lab_config_by_assignment_id(assignment_id)
    if not lab:
        return jsonify(error="Unknown assignment"), 400

    # Make sure the requested filename is one we expect
    expected_files = lab["code_files"] + lab.get("writeup_files", [])
    if filename not in expected_files:
        return jsonify(error=f"Unexpected filename: {filename}"), 400

    if "file" not in request.files:
        return jsonify(error="No file provided"), 400

    file = request.files["file"]
    if file.filename == "":
        return jsonify(error="Empty filename"), 400

    # Validate extension
    ext = filename.rsplit(".", 1)[1].lower() if "." in filename else ""
    if ext in ALLOWED_CODE_EXTENSIONS:
        pass  # ok
    elif ext in ALLOWED_DOC_EXTENSIONS:
        pass  # ok
    else:
        return jsonify(error=f"File type .{ext} not allowed"), 400

    student_folder = get_submission_folder(session["student_id"], assignment_id)
    dest = os.path.join(student_folder, filename)
    file.save(dest)

    logging.info(
        "Student %s uploaded %s for assignment %s",
        session["student_id"], filename, assignment_id,
    )
    return jsonify(success=True)


@app.route("/revert/<assignment_id>/<filename>", methods=["POST"])
def revert_file(assignment_id, filename):
    """Delete the student's uploaded version so the template default is used."""
    if "student_id" not in session:
        return jsonify(error="Not authenticated"), 403

    lab = get_lab_config_by_assignment_id(assignment_id)
    if not lab:
        return jsonify(error="Unknown assignment"), 400

    if filename not in lab["code_files"]:
        return jsonify(error="Cannot revert this file"), 400

    student_folder = get_submission_folder(session["student_id"], assignment_id)
    fpath = os.path.join(student_folder, filename)

    if os.path.isfile(fpath):
        os.remove(fpath)
        logging.info(
            "Student %s reverted %s for assignment %s",
            session["student_id"], filename, assignment_id,
        )
        return jsonify(success=True, message=f"{filename} reverted to template default.")
    else:
        return jsonify(success=True, message=f"{filename} was already using the template default.")


@app.route("/view/<assignment_id>/<filename>")
def view_file(assignment_id, filename):
    """View the student's uploaded version of a file."""
    if "student_id" not in session:
        return redirect(url_for("login"))

    lab = get_lab_config_by_assignment_id(assignment_id)
    if not lab:
        flash("Unknown assignment")
        return redirect(url_for("home"))

    student_folder = get_submission_folder(session["student_id"], assignment_id)
    fpath = os.path.join(student_folder, filename)

    if not os.path.isfile(fpath):
        flash("File not found")
        return redirect(url_for("assignment", assignment_id=assignment_id))

    with open(fpath, "r", errors="replace") as f:
        content = f.read()

    return render_template("view_file.html", filename=filename, content=content)


@app.route("/view-template/<lab_name>/<filename>")
def view_template(lab_name, filename):
    """View the original template version of a file (read-only)."""
    if "student_id" not in session:
        return redirect(url_for("login"))

    # Sanitize to prevent path traversal
    lab_name = secure_filename(lab_name)
    filename = secure_filename(filename)

    fpath = get_template_file_path(lab_name, filename)

    if not os.path.isfile(fpath):
        flash("Template file not found")
        return redirect(url_for("home"))

    with open(fpath, "r", errors="replace") as f:
        content = f.read()

    return render_template("view_file.html", filename=f"{filename} (template)", content=content)

# =============================================================================
# ROUTES – SUBMISSION
# =============================================================================

@app.route("/submit/<assignment_id>", methods=["POST"])
def submit(assignment_id):
    """
    Build a zip of template + student files, then upload to Canvas
    as a submission for this assignment.
    """
    if "student_id" not in session:
        return jsonify(error="Not authenticated"), 403

    lab = get_lab_config_by_assignment_id(assignment_id)
    if not lab:
        return jsonify(error="Unknown assignment"), 400

    student_folder = get_submission_folder(session["student_id"], assignment_id)

    # Require at least one writeup file
    has_writeup = any(
        os.path.isfile(os.path.join(student_folder, wf))
        for wf in lab.get("writeup_files", [])
    )
    if not has_writeup:
        return jsonify(error="Please upload a writeup file before submitting."), 400

    try:
        zip_buf = create_submission_zip(student_folder, lab)
        zip_filename = f"{lab['display_name'].replace(' ', '_')}_{session['student_id']}.zip"

        # ---- Canvas file-upload workflow (3 steps) ----

        # Step 1: Tell Canvas we want to upload a file for this submission
        student_id = session["student_id"]
        preflight = canvas_api_request(
            f"courses/{COURSE_ID}/assignments/{assignment_id}"
            f"/submissions/{student_id}/files",
            method="POST",
            data={
                "name": zip_filename,
                "size": zip_buf.getbuffer().nbytes,
                "content_type": "application/zip",
            },
        )

        upload_url = preflight["upload_url"]
        upload_params = preflight.get("upload_params", {})

        # Step 2: POST the file to the URL Canvas gave us
        zip_buf.seek(0)
        resp = requests.post(
            upload_url,
            data=upload_params,
            files={"file": (zip_filename, zip_buf, "application/zip")},
            timeout=60,
        )
        resp.raise_for_status()
        file_data = resp.json()
        file_id = file_data["id"]

        # Step 3: Create the submission referencing the uploaded file
        canvas_api_request(
            f"courses/{COURSE_ID}/assignments/{assignment_id}/submissions",
            method="POST",
            data={
                "submission": {
                    "submission_type": "online_upload",
                    "file_ids": [file_id],
                }
            },
        )

        logging.info(
            "Student %s submitted assignment %s (file_id=%s)",
            student_id, assignment_id, file_id,
        )
        return jsonify(success=True, message="Submitted successfully to Canvas!")

    except Exception as e:
        logging.error("Submission failed for student %s: %s", session["student_id"], e)
        return jsonify(error=f"Submission failed: {str(e)}"), 500

# =============================================================================
# ROUTES – COMPILATION
# =============================================================================

@app.route("/compile/<assignment_id>", methods=["POST"])
def compile_start(assignment_id):
    if "student_id" not in session:
        return jsonify(error="Not authenticated"), 403
    if not compile_queue.is_available():
        return jsonify(error="Compilation service unavailable"), 503

    assignment_data = canvas_api_request(
        f"courses/{COURSE_ID}/assignments/{assignment_id}"
    )
    lab = get_lab_config_by_assignment_id(assignment_id)
    if not lab:
        return jsonify(error="No lab configuration found"), 400

    # Prepare a merged build directory (templates + student uploads)
    student_folder = get_submission_folder(session["student_id"], assignment_id)
    build_dir = prepare_build_directory(student_folder, lab)

    job_id = compile_queue.submit_job(
        student_id=session["student_id"],
        student_name=session["student_name"],
        netid=session.get("netid", ""),
        assignment_id=assignment_id,
        assignment_name=assignment_data["name"],
        lab_config=json.dumps(lab),   # serialize for Redis
        lab_name=lab["template_dir"],
        build_dir=build_dir,
    )

    return jsonify(success=True, job_id=job_id)

@app.route("/compile-status/<job_id>")
def compile_status(job_id):
    status = compile_queue.get_job_status(job_id)
    if not status:
        return jsonify(error="Not found"), 404
    return jsonify(status)

@app.route("/compile-cancel/<job_id>", methods=["POST"])
def compile_cancel(job_id):
    if "student_id" not in session:
        return jsonify(error="Not authenticated"), 403

    result = compile_queue.cancel_job(job_id, session["student_id"])
    if result["success"]:
        return jsonify(result)
    else:
        return jsonify(result), 400

# =============================================================================
# ROUTES – ADMIN
# =============================================================================

@app.route("/admin/login", methods=["GET", "POST"])
def admin_login():
    if request.method == "POST":
        password = request.form.get("password", "")
        if password == ADMIN_PASSWORD:
            session["admin_authenticated"] = True
            return redirect(url_for("admin_compile_queue"))
        flash("Invalid password")
    return render_template("admin_login.html")

@app.route("/admin/compile-queue")
def admin_compile_queue():
    """Admin dashboard for the compilation queue."""
    # Check auth: via session or query param (for initial login from admin_login.html)
    password_param = request.args.get("password")
    if password_param:
        if password_param == ADMIN_PASSWORD:
            session["admin_authenticated"] = True
        else:
            flash("Invalid password")
            return render_template("admin_login.html")

    if not session.get("admin_authenticated"):
        return render_template("admin_login.html")

    if not compile_queue.is_available():
        flash("Compilation queue unavailable (Redis not connected)")
        jobs, queued_count, compiling_count = [], 0, 0
    else:
        jobs = compile_queue.get_full_queue()
        queued_count = len([j for j in jobs if j.get("state") == "queued"])
        compiling_count = len([j for j in jobs if j.get("state") == "compiling"])

    return render_template(
        "admin_queue.html",
        jobs=jobs,
        queued_count=queued_count,
        compiling_count=compiling_count,
        max_workers=compile_queue.max_workers,
    )

@app.route("/admin/compile-queue/data")
def admin_queue_data():
    """JSON endpoint for AJAX auto-refresh of the admin dashboard."""
    if not session.get("admin_authenticated"):
        return jsonify(error="Not authenticated"), 403

    if not compile_queue.is_available():
        return jsonify(jobs=[], queued_count=0, compiling_count=0)

    jobs = compile_queue.get_full_queue()
    queued_count = len([j for j in jobs if j.get("state") == "queued"])
    compiling_count = len([j for j in jobs if j.get("state") == "compiling"])

    return jsonify(
        jobs=jobs,
        queued_count=queued_count,
        compiling_count=compiling_count,
    )

@app.route("/admin/reload-roster", methods=["POST"])
def admin_reload_roster():
    """Reload the student roster CSV without restarting the server."""
    if not session.get("admin_authenticated"):
        return jsonify(error="Not authenticated"), 403

    count = load_roster(GRADEBOOK_CSV_PATH)
    return jsonify(success=True, students_loaded=count)

# =============================================================================
# ROUTES – DEBUG / HEALTH
# =============================================================================

@app.route("/health")
def health():
    """Quick health check endpoint for monitoring."""
    redis_ok = compile_queue.is_available()
    queue_len = 0
    active_count = 0

    if redis_ok:
        try:
            queue_len = compile_queue.redis.llen("compile_queue")
            active_count = compile_queue.redis.scard("compile_active")
        except Exception:
            pass

    return jsonify(
        status="ok",
        redis_connected=redis_ok,
        queued_jobs=queue_len,
        active_jobs=active_count,
        roster_loaded=len(STUDENT_ROSTER),
    )

# =============================================================================
# MAIN
# =============================================================================

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=False)
