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
import hashlib
import yaml
import markdown as md_lib

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
ROSTER_CSV_PATH = os.environ.get("ROSTER_CSV_PATH", "student_passwords.csv")

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

load_roster(ROSTER_CSV_PATH)


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
# LAB CONFIGURATION (auto-discovered from template_files/*/lab.yaml)
# =============================================================================

def load_lab_configs(template_folder):
    """
    Scan template_files/ for subdirectories containing a lab.yaml.
    Each lab.yaml must have:
        display_name: "Lab 3"
        canvas_assignment_id: "505415"
    And optionally:
        writeup_files:
          - writeup.txt
          - writeup.pdf

    Code files (.c, .h) are auto-discovered from the directory contents.
    Other files (.cmd, etc.) are treated as build infrastructure — copied
    into builds automatically but not shown to students as editable.
    """
    configs = {}

    if not os.path.isdir(template_folder):
        logging.warning("Template folder %s not found", template_folder)
        return configs

    for dirname in sorted(os.listdir(template_folder)):
        lab_dir = os.path.join(template_folder, dirname)
        yaml_path = os.path.join(lab_dir, "lab.yaml")

        if not os.path.isfile(yaml_path):
            continue

        try:
            with open(yaml_path, "r") as f:
                meta = yaml.safe_load(f)
        except Exception as e:
            logging.error("Failed to parse %s: %s", yaml_path, e)
            continue

        if not meta or not isinstance(meta, dict):
            logging.error("Invalid lab.yaml in %s — must be a YAML mapping", dirname)
            continue

        assignment_id = str(meta.get("canvas_assignment_id", "")).strip()
        display_name = meta.get("display_name", dirname)

        if not assignment_id:
            logging.error("lab.yaml in %s missing canvas_assignment_id — skipping", dirname)
            continue

        # Auto-discover code files from directory contents
        code_files = sorted(
            f for f in os.listdir(lab_dir)
            if f.endswith((".c", ".h")) and f != "lab.yaml"
        )

        writeup_files = meta.get("writeup_files", ["writeup.txt", "writeup.pdf"])

        configs[assignment_id] = {
            "display_name": display_name,
            "template_dir": dirname,
            "code_files": code_files,
            "writeup_files": writeup_files,
            "instructions": meta.get("instructions", ""),
        }

        logging.info(
            "Loaded lab: %s (%s) — %d code files, assignment_id=%s",
            display_name, dirname, len(code_files), assignment_id,
        )

    if not configs:
        logging.warning("No lab configurations found in %s/", template_folder)

    return configs


LAB_CONFIGS = load_lab_configs(TEMPLATE_FOLDER)

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

    Returns a dict with three keys:
      "template_files": ordered dict of template code files
          filename -> {
              "uploaded": bool,     # student has their own version
              "excluded": bool,     # student has excluded this template file
              "size": int,
              "modified": str,
          }
      "extra_files": ordered dict of student-added .c/.h files not in template
          filename -> { "size": int, "modified": str }
      "writeup_files": ordered dict of writeup files
          filename -> { "uploaded": bool, "size": int, "modified": str }
    """
    template_status = {}
    for fname in lab_config["code_files"]:
        fpath = os.path.join(student_folder, fname)
        excluded_marker = os.path.join(student_folder, fname + ".excluded")
        if os.path.isfile(fpath):
            stat = os.stat(fpath)
            template_status[fname] = {
                "uploaded": True,
                "excluded": False,
                "size": stat.st_size,
                "modified": datetime.fromtimestamp(stat.st_mtime).strftime("%Y-%m-%d %H:%M"),
            }
        else:
            template_status[fname] = {
                "uploaded": False,
                "excluded": os.path.isfile(excluded_marker),
                "size": 0,
                "modified": "",
            }

    # Discover extra .c/.h files the student added
    known_files = set(lab_config["code_files"]) | set(lab_config.get("writeup_files", []))
    extra_status = {}
    if os.path.isdir(student_folder):
        for fname in sorted(os.listdir(student_folder)):
            if fname in known_files or fname.endswith(".excluded"):
                continue
            ext = fname.rsplit(".", 1)[1].lower() if "." in fname else ""
            if ext in ALLOWED_CODE_EXTENSIONS:
                stat = os.stat(os.path.join(student_folder, fname))
                extra_status[fname] = {
                    "size": stat.st_size,
                    "modified": datetime.fromtimestamp(stat.st_mtime).strftime("%Y-%m-%d %H:%M"),
                }

    # Writeup files
    writeup_status = {}
    for fname in lab_config.get("writeup_files", []):
        fpath = os.path.join(student_folder, fname)
        if os.path.isfile(fpath):
            stat = os.stat(fpath)
            writeup_status[fname] = {
                "uploaded": True,
                "size": stat.st_size,
                "modified": datetime.fromtimestamp(stat.st_mtime).strftime("%Y-%m-%d %H:%M"),
            }
        else:
            writeup_status[fname] = {"uploaded": False, "size": 0, "modified": ""}

    return {
        "template_files": template_status,
        "extra_files": extra_status,
        "writeup_files": writeup_status,
    }


def get_excluded_files(student_folder):
    """Return set of filenames that the student has excluded."""
    excluded = set()
    if os.path.isdir(student_folder):
        for fname in os.listdir(student_folder):
            if fname.endswith(".excluded"):
                excluded.add(fname[:-9])  # strip .excluded
    return excluded


def get_extra_files(student_folder, lab_config):
    """Return list of student-added .c/.h filenames not in the template."""
    known = set(lab_config["code_files"]) | set(lab_config.get("writeup_files", []))
    extras = []
    if os.path.isdir(student_folder):
        for fname in sorted(os.listdir(student_folder)):
            if fname in known or fname.endswith(".excluded"):
                continue
            ext = fname.rsplit(".", 1)[1].lower() if "." in fname else ""
            if ext in ALLOWED_CODE_EXTENSIONS:
                extras.append(fname)
    return extras


def prepare_build_directory(student_folder, lab_config):
    """
    Create a temporary build directory that merges template files with
    student uploads, respecting exclusions and including extra files.

    Returns the path to the build directory.
    """
    import tempfile

    build_dir = tempfile.mkdtemp(prefix="dali_build_")
    template_dir = lab_config["template_dir"]
    excluded = get_excluded_files(student_folder)

    # Template code files (student upload wins; skip excluded)
    for fname in lab_config["code_files"]:
        if fname in excluded:
            continue
        student_path = os.path.join(student_folder, fname)
        template_path = get_template_file_path(template_dir, fname)

        if os.path.isfile(student_path):
            shutil.copy2(student_path, os.path.join(build_dir, fname))
        elif os.path.isfile(template_path):
            shutil.copy2(template_path, os.path.join(build_dir, fname))
        else:
            logging.warning("File %s missing from both student dir and templates", fname)

    # Extra files added by the student
    for fname in get_extra_files(student_folder, lab_config):
        shutil.copy2(os.path.join(student_folder, fname), os.path.join(build_dir, fname))

    # Copy linker script and other non-.c/.h files from template
    template_full_dir = os.path.join(TEMPLATE_FOLDER, template_dir)
    for fname in os.listdir(template_full_dir):
        dest = os.path.join(build_dir, fname)
        if not os.path.exists(dest):
            shutil.copy2(os.path.join(template_full_dir, fname), dest)

    return build_dir


def create_submission_zip(student_folder, lab_config):
    """
    Build an in-memory zip archive that merges template defaults with
    student uploads, respecting exclusions and including extras.
    Returns a BytesIO object.
    """
    buf = io.BytesIO()
    template_dir = lab_config["template_dir"]
    excluded = get_excluded_files(student_folder)

    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        # Template code files (student upload wins; skip excluded)
        for fname in lab_config["code_files"]:
            if fname in excluded:
                continue
            student_path = os.path.join(student_folder, fname)
            template_path = get_template_file_path(template_dir, fname)

            if os.path.isfile(student_path):
                zf.write(student_path, fname)
            elif os.path.isfile(template_path):
                zf.write(template_path, fname)

        # Extra files added by the student
        for fname in get_extra_files(student_folder, lab_config):
            zf.write(os.path.join(student_folder, fname), fname)

        # Writeup (student must provide)
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
            # Store a fingerprint so we can detect password changes
            session["_pw_fp"] = hashlib.sha256(
                student["password"].encode()
            ).hexdigest()[:16]
            logging.info("Login: %s (%s)", student["netid"], student["name"])
            return redirect(url_for("home"))

        flash("Invalid NetID or password.")
    return render_template("login_api.html")

@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))

# ---- Session validation ----
# If the roster is reloaded and a student's password changes (or they're
# removed), their existing session is invalidated on the next request.

@app.before_request
def validate_session():
    """Check that the logged-in student still has valid credentials."""
    # Skip for routes that don't need auth
    if request.endpoint in ("login", "logout", "health", "admin_login", "static", None):
        return

    netid = session.get("netid")
    if not netid:
        return  # not logged in as a student; admin routes handle their own auth

    student = STUDENT_ROSTER.get(netid)
    if not student or student["canvas_id"] != session.get("student_id"):
        # Student removed from roster or canvas_id changed
        session.clear()
        flash("Your session has expired. Please log in again.")
        return redirect(url_for("login"))

    # Check password fingerprint — invalidates session if password was changed
    current_fp = hashlib.sha256(student["password"].encode()).hexdigest()[:16]
    if session.get("_pw_fp") != current_fp:
        session.clear()
        flash("Your password has been changed. Please log in again.")
        return redirect(url_for("login"))

# =============================================================================
# ROUTES – MAIN
# =============================================================================

@app.route("/")
def home():
    if "student_id" not in session:
        return redirect(url_for("login"))
    all_assignments = canvas_api_request(f"courses/{COURSE_ID}/assignments")
    # Only show assignments that have a matching lab configuration
    assignments = [a for a in all_assignments if str(a["id"]) in LAB_CONFIGS]
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
    file_status = build_uploaded_files_status(student_folder, lab)

    # Render markdown instructions to HTML (empty string if none)
    instructions_raw = lab.get("instructions", "")
    instructions_html = md_lib.markdown(instructions_raw) if instructions_raw else ""

    return render_template(
        "assignment_api.html",
        assignment_id=assignment_id,
        assignment_title=assignment_data.get("name", "Assignment"),
        student_name=session["student_name"],
        lab_name=lab["template_dir"],
        code_files=lab["code_files"],
        writeup_files=lab.get("writeup_files", []),
        template_files=file_status["template_files"],
        extra_files=file_status["extra_files"],
        writeup_status=file_status["writeup_files"],
        instructions=instructions_html,
        compile_available=compile_queue.is_available(),
    )

# =============================================================================
# ROUTES – FILE MANAGEMENT
# =============================================================================

@app.route("/upload/<assignment_id>/<filename>", methods=["POST"])
def upload_file(assignment_id, filename):
    """Accept a single file upload. Allows both template files and new extra files."""
    if "student_id" not in session:
        return jsonify(error="Not authenticated"), 403

    lab = get_lab_config_by_assignment_id(assignment_id)
    if not lab:
        return jsonify(error="Unknown assignment"), 400

    if "file" not in request.files:
        return jsonify(error="No file provided"), 400

    file = request.files["file"]
    if file.filename == "":
        return jsonify(error="Empty filename"), 400

    # Validate extension
    ext = filename.rsplit(".", 1)[1].lower() if "." in filename else ""
    if ext not in ALLOWED_CODE_EXTENSIONS and ext not in ALLOWED_DOC_EXTENSIONS:
        return jsonify(error=f"File type .{ext} not allowed"), 400

    # Sanitize — no path traversal
    filename = secure_filename(filename)
    if not filename:
        return jsonify(error="Invalid filename"), 400

    student_folder = get_submission_folder(session["student_id"], assignment_id)
    dest = os.path.join(student_folder, filename)
    file.save(dest)

    # If this was an excluded template file being re-uploaded, remove the marker
    excluded_marker = dest + ".excluded"
    if os.path.isfile(excluded_marker):
        os.remove(excluded_marker)

    logging.info(
        "Student %s uploaded %s for assignment %s",
        session.get("netid"), filename, assignment_id,
    )
    return jsonify(success=True)


@app.route("/upload-extra/<assignment_id>", methods=["POST"])
def upload_extra_file(assignment_id):
    """Upload a new file not in the template. Filename comes from the uploaded file."""
    if "student_id" not in session:
        return jsonify(error="Not authenticated"), 403

    lab = get_lab_config_by_assignment_id(assignment_id)
    if not lab:
        return jsonify(error="Unknown assignment"), 400

    if "file" not in request.files:
        return jsonify(error="No file provided"), 400

    file = request.files["file"]
    if file.filename == "":
        return jsonify(error="Empty filename"), 400

    filename = secure_filename(file.filename)
    if not filename:
        return jsonify(error="Invalid filename"), 400

    ext = filename.rsplit(".", 1)[1].lower() if "." in filename else ""
    if ext not in ALLOWED_CODE_EXTENSIONS:
        return jsonify(error=f"Only .c and .h files allowed, got .{ext}"), 400

    student_folder = get_submission_folder(session["student_id"], assignment_id)
    dest = os.path.join(student_folder, filename)
    file.save(dest)

    logging.info(
        "Student %s uploaded extra file %s for assignment %s",
        session.get("netid"), filename, assignment_id,
    )
    return jsonify(success=True, filename=filename)


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
            session.get("netid"), filename, assignment_id,
        )
        return jsonify(success=True, message=f"{filename} reverted to template default.")
    else:
        return jsonify(success=True, message=f"{filename} was already using the template default.")


@app.route("/exclude/<assignment_id>/<filename>", methods=["POST"])
def exclude_file(assignment_id, filename):
    """Exclude a template file from the build (without deleting student upload if any)."""
    if "student_id" not in session:
        return jsonify(error="Not authenticated"), 403

    lab = get_lab_config_by_assignment_id(assignment_id)
    if not lab:
        return jsonify(error="Unknown assignment"), 400

    if filename not in lab["code_files"]:
        return jsonify(error="Can only exclude template files"), 400

    student_folder = get_submission_folder(session["student_id"], assignment_id)

    # Remove any student upload for this file
    fpath = os.path.join(student_folder, filename)
    if os.path.isfile(fpath):
        os.remove(fpath)

    # Create exclusion marker
    marker = os.path.join(student_folder, filename + ".excluded")
    with open(marker, "w") as f:
        f.write("")

    logging.info(
        "Student %s excluded %s for assignment %s",
        session.get("netid"), filename, assignment_id,
    )
    return jsonify(success=True, message=f"{filename} excluded from build.")


@app.route("/restore/<assignment_id>/<filename>", methods=["POST"])
def restore_file(assignment_id, filename):
    """Restore a previously excluded template file."""
    if "student_id" not in session:
        return jsonify(error="Not authenticated"), 403

    lab = get_lab_config_by_assignment_id(assignment_id)
    if not lab:
        return jsonify(error="Unknown assignment"), 400

    if filename not in lab["code_files"]:
        return jsonify(error="Can only restore template files"), 400

    student_folder = get_submission_folder(session["student_id"], assignment_id)
    marker = os.path.join(student_folder, filename + ".excluded")

    if os.path.isfile(marker):
        os.remove(marker)

    logging.info(
        "Student %s restored %s for assignment %s",
        session.get("netid"), filename, assignment_id,
    )
    return jsonify(success=True, message=f"{filename} restored to build.")


@app.route("/delete-extra/<assignment_id>/<filename>", methods=["POST"])
def delete_extra_file(assignment_id, filename):
    """Delete a student-added extra file."""
    if "student_id" not in session:
        return jsonify(error="Not authenticated"), 403

    lab = get_lab_config_by_assignment_id(assignment_id)
    if not lab:
        return jsonify(error="Unknown assignment"), 400

    # Only allow deleting files that are NOT template files
    if filename in lab["code_files"] or filename in lab.get("writeup_files", []):
        return jsonify(error="Use revert for template files"), 400

    filename = secure_filename(filename)
    student_folder = get_submission_folder(session["student_id"], assignment_id)
    fpath = os.path.join(student_folder, filename)

    if os.path.isfile(fpath):
        os.remove(fpath)
        logging.info(
            "Student %s deleted extra file %s for assignment %s",
            session.get("netid"), filename, assignment_id,
        )
        return jsonify(success=True, message=f"{filename} deleted.")
    else:
        return jsonify(error="File not found"), 404


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
    Build a zip of template + student files, then upload it to Canvas
    as a submission comment attachment.
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
        netid = session.get("netid", session["student_id"])
        zip_filename = f"{lab['display_name'].replace(' ', '_')}_{netid}.zip"
        student_id = session["student_id"]

        # ---- Canvas submission-comment file upload (3 steps) ----

        # Step 1: Preflight — tell Canvas we want to upload a file
        #         for a submission comment
        preflight = canvas_api_request(
            f"courses/{COURSE_ID}/assignments/{assignment_id}"
            f"/submissions/{student_id}/comments/files",
            method="POST",
            data={
                "name": zip_filename,
                "size": zip_buf.getbuffer().nbytes,
                "content_type": "application/zip",
            },
        )

        upload_url = preflight["upload_url"]
        upload_params = preflight.get("upload_params", {})

        # Step 2: Upload the actual file to the URL Canvas gave us
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

        # Step 3: Create the submission comment with the file attached
        timestamp = datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")
        canvas_api_request(
            f"courses/{COURSE_ID}/assignments/{assignment_id}"
            f"/submissions/{student_id}",
            method="PUT",
            data={
                "comment": {
                    "text_comment": f"Submitted via DALI at {timestamp}",
                    "file_ids": [file_id],
                }
            },
        )

        logging.info(
            "Student %s (%s) submitted assignment %s as comment (file_id=%s)",
            netid, student_id, assignment_id, file_id,
        )
        return jsonify(success=True, message="Submitted successfully to Canvas!")

    except requests.exceptions.HTTPError as e:
        # Log the Canvas error response for debugging
        error_body = ""
        if e.response is not None:
            try:
                error_body = e.response.json()
            except Exception:
                error_body = e.response.text[:500]
        logging.error(
            "Canvas API error for student %s on assignment %s: %s — %s",
            session.get("netid"), assignment_id, e, error_body,
        )
        return jsonify(error=f"Canvas rejected the submission: {e}"), 500

    except Exception as e:
        logging.error("Submission failed for student %s: %s", session.get("netid"), e)
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
        labs_loaded={aid: cfg["display_name"] for aid, cfg in LAB_CONFIGS.items()},
    )

# =============================================================================
# MAIN
# =============================================================================

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=False)
