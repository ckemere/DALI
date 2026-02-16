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
from pcb_makefile_generator import create_makefile_for_pcb  # NEW: PCB support

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
ALLOWED_PCB_EXTENSIONS = {"kicad_pcb", "kicad_sch", "kicad_pro", "kicad_dru"}  # NEW
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

    Supports two assignment types:

    type: embedded_c (default)
        Code files (.c, .h) are auto-discovered from the directory.
        Other files (.cmd, etc.) are build infrastructure.

    type: kicad_pcb
        Student uploads KiCad project files (.kicad_pcb, etc.).
        DRU files are specified in the YAML and live in the template dir.
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

        writeup_files = meta.get("writeup_files", [])
        lab_type = meta.get("type", "embedded_c")

        if lab_type == "kicad_pcb":
            # ----- PCB assignment -----
            dru_files = meta.get("dru_files", [])

            # Validate DRU files exist
            for dru in dru_files:
                dru_path = os.path.join(lab_dir, dru["name"])
                if not os.path.isfile(dru_path):
                    logging.error(
                        "DRU file %s not found in %s", dru["name"], dirname
                    )

            configs[assignment_id] = {
                "display_name": display_name,
                "template_dir": dirname,
                "type": "kicad_pcb",
                "code_files": [],  # no .c/.h files for PCB labs
                "dru_files": dru_files,
                "writeup_files": writeup_files,
                "instructions": meta.get("instructions", ""),
                "scoring": meta.get("scoring", None),
            }

            logging.info(
                "Loaded PCB lab: %s (%s) — %d DRU files, assignment_id=%s",
                display_name, dirname, len(dru_files), assignment_id,
            )

        else:
            # ----- Embedded C assignment (original behavior) -----
            code_files = sorted(
                f for f in os.listdir(lab_dir)
                if f.endswith((".c", ".h")) and f != "lab.yaml"
            )

            configs[assignment_id] = {
                "display_name": display_name,
                "template_dir": dirname,
                "type": "embedded_c",
                "code_files": code_files,
                "writeup_files": writeup_files,
                "instructions": meta.get("instructions", ""),
                "scoring": meta.get("scoring", None),
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


def get_allowed_extensions(lab_config):
    """Return the set of allowed file extensions for this lab type."""
    lab_type = lab_config.get("type", "embedded_c")
    if lab_type == "kicad_pcb":
        return ALLOWED_PCB_EXTENSIONS | ALLOWED_DOC_EXTENSIONS
    else:
        return ALLOWED_CODE_EXTENSIONS | ALLOWED_DOC_EXTENSIONS


# Extensions where only one file of each type should exist at a time.
# When a student uploads e.g. a new .kicad_pcb, any existing .kicad_pcb is removed.
PCB_SINGLETON_EXTENSIONS = {"kicad_pcb", "kicad_sch", "kicad_pro"}


def _remove_existing_pcb_file(student_folder, ext):
    """
    Remove any existing file with the given extension from the student folder.

    KiCad expects exactly one .kicad_pcb (and matching .kicad_sch / .kicad_pro)
    per project directory.  If a student re-uploads with a different filename,
    the old file must be cleaned up so there's never more than one of each type.
    """
    if ext not in PCB_SINGLETON_EXTENSIONS:
        return
    if not os.path.isdir(student_folder):
        return
    for fname in os.listdir(student_folder):
        if fname.startswith("_"):
            continue
        fext = fname.rsplit(".", 1)[1].lower() if "." in fname else ""
        if fext == ext:
            old_path = os.path.join(student_folder, fname)
            os.remove(old_path)
            logging.info("Removed previous .%s file: %s", ext, fname)


def build_uploaded_files_status(student_folder, lab_config):
    """
    Build the status dict the assignment template expects.

    For embedded_c: template_files, extra_files, writeup_files
    For kicad_pcb:  pcb_files, writeup_files
    """
    lab_type = lab_config.get("type", "embedded_c")

    if lab_type == "kicad_pcb":
        return _build_pcb_files_status(student_folder, lab_config)
    else:
        return _build_embedded_c_files_status(student_folder, lab_config)


def _build_embedded_c_files_status(student_folder, lab_config):
    """Original embedded C file status builder."""
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
            if fname.startswith("_"):
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


def _build_pcb_files_status(student_folder, lab_config):
    """Build file status for PCB assignments."""
    pcb_info = {"uploaded": False, "filename": None, "size": 0, "modified": ""}
    sch_info = {"uploaded": False, "filename": None, "size": 0, "modified": ""}
    pro_info = {"uploaded": False, "filename": None, "size": 0, "modified": ""}

    if os.path.isdir(student_folder):
        for fname in os.listdir(student_folder):
            if fname.startswith("_"):
                continue
            ext = fname.rsplit(".", 1)[1].lower() if "." in fname else ""
            fpath = os.path.join(student_folder, fname)
            stat = os.stat(fpath)
            info = {
                "uploaded": True,
                "filename": fname,
                "size": stat.st_size,
                "modified": datetime.fromtimestamp(stat.st_mtime).strftime("%Y-%m-%d %H:%M"),
            }
            if ext == "kicad_pcb":
                pcb_info = info
            elif ext == "kicad_sch":
                sch_info = info
            elif ext == "kicad_pro":
                pro_info = info

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
        "pcb": pcb_info,
        "sch": sch_info,
        "pro": pro_info,
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
            if fname.startswith("_"):
                continue
            ext = fname.rsplit(".", 1)[1].lower() if "." in fname else ""
            if ext in ALLOWED_CODE_EXTENSIONS:
                extras.append(fname)
    return extras


# -- Compile status tracking --------------------------------------------------

COMPILE_STATUS_FILE = "_compile_status.json"

def compute_file_fingerprint(student_folder, lab_config):
    """
    Compute a hash representing the current state of all relevant files.
    Any change invalidates the fingerprint.
    """
    lab_type = lab_config.get("type", "embedded_c")
    h = hashlib.sha256()

    if lab_type == "kicad_pcb":
        # Hash all PCB-related files
        if os.path.isdir(student_folder):
            for fname in sorted(os.listdir(student_folder)):
                if fname.startswith("_"):
                    continue
                ext = fname.rsplit(".", 1)[1].lower() if "." in fname else ""
                if ext in ALLOWED_PCB_EXTENSIONS or ext in ALLOWED_DOC_EXTENSIONS:
                    fpath = os.path.join(student_folder, fname)
                    stat = os.stat(fpath)
                    h.update(f"{fname}:{stat.st_size}:{stat.st_mtime_ns}\n".encode())
    else:
        # Original embedded_c fingerprinting
        excluded = get_excluded_files(student_folder)

        for fname in lab_config["code_files"]:
            if fname in excluded:
                h.update(f"{fname}:excluded\n".encode())
            else:
                fpath = os.path.join(student_folder, fname)
                if os.path.isfile(fpath):
                    stat = os.stat(fpath)
                    h.update(f"{fname}:uploaded:{stat.st_size}:{stat.st_mtime_ns}\n".encode())
                else:
                    h.update(f"{fname}:template\n".encode())

        for fname in get_extra_files(student_folder, lab_config):
            fpath = os.path.join(student_folder, fname)
            stat = os.stat(fpath)
            h.update(f"{fname}:extra:{stat.st_size}:{stat.st_mtime_ns}\n".encode())

    return h.hexdigest()[:16]


def save_compile_status(student_folder, lab_config, success):
    """Save compile result with current file fingerprint."""
    status = {
        "success": success,
        "fingerprint": compute_file_fingerprint(student_folder, lab_config),
        "timestamp": datetime.utcnow().isoformat(),
    }
    path = os.path.join(student_folder, COMPILE_STATUS_FILE)
    with open(path, "w") as f:
        json.dump(status, f)


def get_compile_status(student_folder, lab_config):
    """
    Return the compile status if it matches the current file state.
    Returns: "passed", "failed", or "untested"
    """
    path = os.path.join(student_folder, COMPILE_STATUS_FILE)
    if not os.path.isfile(path):
        return "untested"
    try:
        with open(path) as f:
            status = json.load(f)
        current_fp = compute_file_fingerprint(student_folder, lab_config)
        if status.get("fingerprint") != current_fp:
            return "untested"
        return "passed" if status.get("success") else "failed"
    except (json.JSONDecodeError, KeyError):
        return "untested"


def prepare_build_directory(student_folder, lab_config):
    """
    Create a temporary build directory that merges template/student files.
    Dispatches based on lab type.
    """
    lab_type = lab_config.get("type", "embedded_c")

    if lab_type == "kicad_pcb":
        return _prepare_pcb_build_directory(student_folder, lab_config)
    else:
        return _prepare_embedded_c_build_directory(student_folder, lab_config)


def _prepare_embedded_c_build_directory(student_folder, lab_config):
    """Original embedded C build directory preparation."""
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

    # Copy linker script, lab.yaml, and other non-code files from template.
    # Explicitly skip code files that the student excluded — without this
    # check, excluded .c/.h files would get copied back into the build
    # directory because they don't yet exist there.
    template_full_dir = os.path.join(TEMPLATE_FOLDER, template_dir)
    for fname in os.listdir(template_full_dir):
        dest = os.path.join(build_dir, fname)
        if os.path.exists(dest):
            continue
        if fname in excluded:
            continue
        shutil.copy2(os.path.join(template_full_dir, fname), dest)

    return build_dir


def _prepare_pcb_build_directory(student_folder, lab_config):
    """
    Create a temp build directory for a PCB DRC job.
    Copies: student's KiCad files + instructor's DRU files from the template.
    """
    import tempfile

    build_dir = tempfile.mkdtemp(prefix="dali_pcb_")
    template_dir = lab_config["template_dir"]

    # Copy student's KiCad files
    if os.path.isdir(student_folder):
        for fname in os.listdir(student_folder):
            if fname.startswith("_"):
                continue
            ext = fname.rsplit(".", 1)[1].lower() if "." in fname else ""
            if ext in ALLOWED_PCB_EXTENSIONS:
                shutil.copy2(
                    os.path.join(student_folder, fname),
                    os.path.join(build_dir, fname),
                )

    # Copy DRU files from template directory
    template_full = os.path.join(TEMPLATE_FOLDER, template_dir)
    for dru in lab_config.get("dru_files", []):
        dru_src = os.path.join(template_full, dru["name"])
        if os.path.isfile(dru_src):
            shutil.copy2(dru_src, os.path.join(build_dir, dru["name"]))
        else:
            logging.warning("DRU file %s not found in %s", dru["name"], template_dir)

    return build_dir


def create_submission_zip(student_folder, lab_config):
    """
    Build an in-memory zip archive for Canvas submission.
    Dispatches based on lab type.
    """
    lab_type = lab_config.get("type", "embedded_c")

    if lab_type == "kicad_pcb":
        return _create_pcb_submission_zip(student_folder, lab_config)
    else:
        return _create_embedded_c_submission_zip(student_folder, lab_config)


def _create_embedded_c_submission_zip(student_folder, lab_config):
    """Original embedded C zip builder."""
    buf = io.BytesIO()
    template_dir = lab_config["template_dir"]
    excluded = get_excluded_files(student_folder)

    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for fname in lab_config["code_files"]:
            if fname in excluded:
                continue
            student_path = os.path.join(student_folder, fname)
            template_path = get_template_file_path(template_dir, fname)

            if os.path.isfile(student_path):
                zf.write(student_path, fname)
            elif os.path.isfile(template_path):
                zf.write(template_path, fname)

        for fname in get_extra_files(student_folder, lab_config):
            zf.write(os.path.join(student_folder, fname), fname)

        for fname in lab_config.get("writeup_files", []):
            student_path = os.path.join(student_folder, fname)
            if os.path.isfile(student_path):
                zf.write(student_path, fname)

    buf.seek(0)
    return buf


def _create_pcb_submission_zip(student_folder, lab_config):
    """Build zip with: KiCad files + DRC reports + preview PNGs + writeup."""
    buf = io.BytesIO()
    results_dir = os.path.join(student_folder, "_pcb_results")

    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        # Student KiCad files
        if os.path.isdir(student_folder):
            for fname in os.listdir(student_folder):
                if fname.startswith("_"):
                    continue
                ext = fname.rsplit(".", 1)[1].lower() if "." in fname else ""
                if ext in ALLOWED_PCB_EXTENSIONS:
                    zf.write(os.path.join(student_folder, fname), fname)

        # DRC reports and previews from last run
        if os.path.isdir(results_dir):
            for fname in os.listdir(results_dir):
                if fname.endswith((".html", ".json", ".png")):
                    zf.write(os.path.join(results_dir, fname), fname)

        # Writeup
        for fname in lab_config.get("writeup_files", []):
            fpath = os.path.join(student_folder, fname)
            if os.path.isfile(fpath):
                zf.write(fpath, fname)

    buf.seek(0)
    return buf


# -- PCB results helpers ------------------------------------------------------

def load_pcb_results(student_folder, lab_config):
    """
    Check for DRC reports and preview PNGs from a previous run.
    Returns dict for the assignment_pcb.html template.
    """
    results_dir = os.path.join(student_folder, "_pcb_results")
    if not os.path.isdir(results_dir):
        return {"ran": False, "drc_reports": [], "preview_top": False, "preview_bottom": False}

    drc_reports = []
    for dru in lab_config.get("dru_files", []):
        slug = os.path.splitext(dru["name"])[0].replace(" ", "_").replace("-", "_")
        html_path = os.path.join(results_dir, f"drc_{slug}.html")
        json_path = os.path.join(results_dir, f"drc_{slug}.json")

        report = {"label": dru.get("label", dru["name"]), "slug": slug}

        if os.path.isfile(html_path):
            report["html_available"] = True
            if os.path.isfile(json_path):
                try:
                    with open(json_path) as f:
                        data = json.load(f)
                    # Import here to use the same logic as the report generator
                    from drc_report_generator import filter_errors
                    errors = filter_errors(data)
                    report["error_count"] = len(errors)
                    report["passed"] = len(errors) == 0
                except Exception:
                    report["passed"] = None
                    report["error_count"] = None
            else:
                report["passed"] = None
                report["error_count"] = None
        else:
            report["html_available"] = False

        drc_reports.append(report)

    preview_top = os.path.isfile(os.path.join(results_dir, "preview_top.png"))
    preview_bottom = os.path.isfile(os.path.join(results_dir, "preview_bottom.png"))

    return {
        "ran": True,
        "drc_reports": drc_reports,
        "preview_top": preview_top,
        "preview_bottom": preview_bottom,
    }


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

@app.before_request
def validate_session():
    """Check that the logged-in student still has valid credentials."""
    if request.endpoint in ("login", "logout", "health", "admin_login", "static", None):
        return

    netid = session.get("netid")
    if not netid:
        return

    student = STUDENT_ROSTER.get(netid)
    if not student or student["canvas_id"] != session.get("student_id"):
        session.clear()
        flash("Your session has expired. Please log in again.")
        return redirect(url_for("login"))

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

    instructions_raw = lab.get("instructions", "")
    instructions_html = md_lib.markdown(instructions_raw) if instructions_raw else ""

    compile_state = get_compile_status(student_folder, lab)
    scoring = lab.get("scoring") or {}
    lab_type = lab.get("type", "embedded_c")

    if lab_type == "kicad_pcb":
        # --- PCB assignment ---
        pcb_results = load_pcb_results(student_folder, lab)

        return render_template(
            "assignment_pcb.html",
            assignment_id=assignment_id,
            assignment_title=assignment_data.get("name", "Assignment"),
            student_name=session["student_name"],
            lab_name=lab["template_dir"],
            dru_files=lab.get("dru_files", []),
            writeup_files=lab.get("writeup_files", []),
            writeup_status=file_status["writeup_files"],
            pcb_uploaded=file_status["pcb"]["uploaded"],
            pcb_filename=file_status["pcb"].get("filename"),
            pcb_size=file_status["pcb"].get("size", 0),
            pcb_modified=file_status["pcb"].get("modified", ""),
            sch_uploaded=file_status["sch"]["uploaded"],
            sch_filename=file_status["sch"].get("filename"),
            sch_size=file_status["sch"].get("size", 0),
            sch_modified=file_status["sch"].get("modified", ""),
            pro_uploaded=file_status["pro"]["uploaded"],
            pro_filename=file_status["pro"].get("filename"),
            pro_size=file_status["pro"].get("size", 0),
            pro_modified=file_status["pro"].get("modified", ""),
            pcb_results=pcb_results,
            instructions=instructions_html,
            compile_state=compile_state,
            scoring=scoring,
            compile_available=compile_queue.is_available(),
        )
    else:
        # --- Embedded C assignment (original) ---
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
            compile_state=compile_state,
            scoring=scoring,
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

    # Validate extension based on lab type
    ext = filename.rsplit(".", 1)[1].lower() if "." in filename else ""
    allowed = get_allowed_extensions(lab)
    if ext not in allowed:
        return jsonify(error=f"File type .{ext} not allowed"), 400

    filename = secure_filename(filename)
    if not filename:
        return jsonify(error="Invalid filename"), 400

    student_folder = get_submission_folder(session["student_id"], assignment_id)

    # For PCB labs, ensure only one file per KiCad type exists at a time.
    # If the student uploads a new .kicad_pcb with a different name than the
    # previous one, remove the old one first.
    if lab.get("type") == "kicad_pcb":
        _remove_existing_pcb_file(student_folder, ext)

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

    # For embedded_c, only .c/.h; for PCB, use PCB extensions
    lab_type = lab.get("type", "embedded_c")
    if lab_type == "kicad_pcb":
        if ext not in ALLOWED_PCB_EXTENSIONS:
            return jsonify(error=f"Only KiCad files allowed, got .{ext}"), 400
    else:
        if ext not in ALLOWED_CODE_EXTENSIONS:
            return jsonify(error=f"Only .c and .h files allowed, got .{ext}"), 400

    student_folder = get_submission_folder(session["student_id"], assignment_id)

    # For PCB labs, ensure only one file per KiCad type
    if lab_type == "kicad_pcb":
        _remove_existing_pcb_file(student_folder, ext)

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
    """Exclude a template file from the build."""
    if "student_id" not in session:
        return jsonify(error="Not authenticated"), 403

    lab = get_lab_config_by_assignment_id(assignment_id)
    if not lab:
        return jsonify(error="Unknown assignment"), 400

    if filename not in lab["code_files"]:
        return jsonify(error="Can only exclude template files"), 400

    student_folder = get_submission_folder(session["student_id"], assignment_id)

    fpath = os.path.join(student_folder, filename)
    if os.path.isfile(fpath):
        os.remove(fpath)

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
# ROUTES – PCB RESULTS (NEW)
# =============================================================================

@app.route("/pcb-results/<assignment_id>/preview/<side>.png")
def pcb_preview(assignment_id, side):
    """Serve a PCB preview PNG."""
    if "student_id" not in session:
        return redirect(url_for("login"))
    if side not in ("top", "bottom"):
        return "Not found", 404

    student_folder = get_submission_folder(session["student_id"], assignment_id)
    img_path = os.path.join(student_folder, "_pcb_results", f"preview_{side}.png")

    if not os.path.isfile(img_path):
        return "Not found", 404
    return send_file(img_path, mimetype="image/png")


@app.route("/pcb-results/<assignment_id>/drc/<slug>.html")
def pcb_drc_report(assignment_id, slug):
    """Serve a DRC HTML report."""
    if "student_id" not in session:
        return redirect(url_for("login"))

    slug = secure_filename(slug)
    student_folder = get_submission_folder(session["student_id"], assignment_id)
    html_path = os.path.join(student_folder, "_pcb_results", f"drc_{slug}.html")

    if not os.path.isfile(html_path):
        return "Not found", 404
    return send_file(html_path, mimetype="text/html")

# =============================================================================
# CANVAS FILE UPLOAD HELPERS
# =============================================================================

# Set to True to submit as an actual Canvas submission (online_upload).
# Set to False to use the old behavior (comment attachment only).
SUBMIT_AS_UPLOAD = os.environ.get("SUBMIT_AS_UPLOAD", "true").lower() in ("true", "1", "yes")


def _canvas_upload_file(preflight_url, filename, zip_buf, as_user_id=None):
    """
    Perform the Canvas 3-step file upload (steps 1 & 2 & 3).

    Step 1: POST preflight to get upload_url + upload_params
    Step 2: POST the file to upload_url
    Step 3: Follow any redirect (3XX) to finalize the file

    When uploading to a student's submission file area using an instructor
    token, the redirect confirmation URL points to the student's file
    context.  The instructor token alone gets a 403 on that URL.  Passing
    as_user_id tells Canvas to masquerade as the student for the
    confirmation GET (and the preflight POST), which resolves this.

    The calling user (instructor) must have the "Become other users"
    permission in Canvas (typically granted to all admins/instructors).

    Returns the file_id of the newly created Canvas file.
    """
    # Build masquerade query string if needed
    masq = f"as_user_id={as_user_id}" if as_user_id else ""

    # Step 1: Preflight
    # Append masquerade to the preflight URL so the file is created in
    # the student's context.
    preflight_endpoint = preflight_url
    if masq:
        sep = "&" if "?" in preflight_endpoint else "?"
        preflight_endpoint = f"{preflight_endpoint}{sep}{masq}"

    preflight = canvas_api_request(
        preflight_endpoint,
        method="POST",
        data={
            "name": filename,
            "size": zip_buf.getbuffer().nbytes,
            "content_type": "application/zip",
        },
    )

    upload_url = preflight["upload_url"]
    upload_params = preflight.get("upload_params", {})

    # Step 2: Upload the actual file
    # No auth token here — the upload_url is pre-signed by Canvas.
    zip_buf.seek(0)
    resp = requests.post(
        upload_url,
        data=upload_params,
        files={"file": (filename, zip_buf, "application/zip")},
        timeout=60,
        allow_redirects=False,  # Handle redirects manually per Canvas docs
    )

    # Step 3: Handle the response
    #   - 3XX redirect: follow it with an authenticated GET to finalize
    #   - 201 Created:  file data may be at Location header (GET it)
    #   - 200 OK:       file data is in the response body directly
    #
    # For the confirmation GET we must also masquerade, because the
    # redirect URL (e.g. /api/v1/files/NNN) is in the student's file
    # context and the instructor token alone is not authorized.
    headers = {"Authorization": f"Bearer {CANVAS_API_TOKEN}"}

    if resp.status_code in (301, 302, 303, 307, 308):
        location = resp.headers["Location"]
        if masq:
            sep = "&" if "?" in location else "?"
            location = f"{location}{sep}{masq}"
        confirm = requests.get(location, headers=headers, timeout=30)
        confirm.raise_for_status()
        file_data = confirm.json()
    elif resp.status_code == 201:
        location = resp.headers.get("Location")
        if location:
            if masq:
                sep = "&" if "?" in location else "?"
                location = f"{location}{sep}{masq}"
            confirm = requests.get(location, headers=headers, timeout=30)
            confirm.raise_for_status()
            file_data = confirm.json()
        else:
            file_data = resp.json()
    else:
        resp.raise_for_status()
        file_data = resp.json()

    return file_data["id"]


def _upload_submission_file(assignment_id, student_id, filename, zip_buf):
    """Upload a file for use as an actual submission (online_upload).

    Uses masquerading (as_user_id) so the file is created in the student's
    context and the instructor token can access the confirmation URL.
    """
    preflight_url = (
        f"courses/{COURSE_ID}/assignments/{assignment_id}"
        f"/submissions/{student_id}/files"
    )
    return _canvas_upload_file(preflight_url, filename, zip_buf, as_user_id=student_id)


def _upload_comment_file(assignment_id, student_id, filename, zip_buf):
    """Upload a file for use as a submission comment attachment."""
    preflight_url = (
        f"courses/{COURSE_ID}/assignments/{assignment_id}"
        f"/submissions/{student_id}/comments/files"
    )
    # Comment file uploads are done as the instructor — no masquerade needed
    # (this was the original working behavior).
    return _canvas_upload_file(preflight_url, filename, zip_buf)


def _create_submission(assignment_id, student_id, file_id, timestamp):
    """
    Create an actual Canvas submission (online_upload) on behalf of the student.

    The API token must belong to a user with grading permission on the course
    (instructor / TA) in order to use submission[user_id] for on-behalf-of
    submission.

    The assignment must include 'online_upload' in its submission_types.
    """
    canvas_api_request(
        f"courses/{COURSE_ID}/assignments/{assignment_id}/submissions",
        method="POST",
        data={
            "submission": {
                "submission_type": "online_upload",
                "file_ids": [file_id],
                "user_id": student_id,
            },
            # Also add a comment so there's an audit trail
            "comment": {
                "text_comment": f"Submitted via DALI at {timestamp}",
            },
        },
    )


def _attach_comment(assignment_id, student_id, file_id, timestamp):
    """Attach a file as a submission comment (original behavior)."""
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

# =============================================================================
# ROUTES – SUBMISSION
# =============================================================================

@app.route("/submit/<assignment_id>", methods=["POST"])
def submit(assignment_id):
    """
    Build a zip of template + student files, then upload it to Canvas.

    If SUBMIT_AS_UPLOAD is True (default), this creates an actual Canvas
    submission of type online_upload, so the zip appears in SpeedGrader
    as the student's submission — not just a comment.

    If SUBMIT_AS_UPLOAD is False, falls back to the original behavior of
    uploading the zip as a submission comment attachment.
    """
    if "student_id" not in session:
        return jsonify(error="Not authenticated"), 403

    lab = get_lab_config_by_assignment_id(assignment_id)
    if not lab:
        return jsonify(error="Unknown assignment"), 400

    student_folder = get_submission_folder(session["student_id"], assignment_id)

    # Require at least one writeup file (only if the lab expects writeups)
    writeup_files = lab.get("writeup_files", [])
    if writeup_files:
        has_writeup = any(
            os.path.isfile(os.path.join(student_folder, wf))
            for wf in writeup_files
        )
        if not has_writeup:
            return jsonify(error="Please upload a writeup file before submitting."), 400

    # For PCB labs, require a .kicad_pcb file
    lab_type = lab.get("type", "embedded_c")
    if lab_type == "kicad_pcb":
        has_pcb = any(
            f.endswith(".kicad_pcb")
            for f in os.listdir(student_folder)
            if not f.startswith("_")
        )
        if not has_pcb:
            return jsonify(error="Please upload a .kicad_pcb file before submitting."), 400

    try:
        zip_buf = create_submission_zip(student_folder, lab)
        netid = session.get("netid", session["student_id"])
        zip_filename = f"{lab['display_name'].replace(' ', '_')}_{netid}.zip"
        student_id = session["student_id"]
        timestamp = datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")

        if SUBMIT_AS_UPLOAD:
            file_id = _upload_submission_file(assignment_id, student_id, zip_filename, zip_buf)
            _create_submission(assignment_id, student_id, file_id, timestamp)
        else:
            file_id = _upload_comment_file(assignment_id, student_id, zip_filename, zip_buf)
            _attach_comment(assignment_id, student_id, file_id, timestamp)

        # ---- Post score if scoring is configured ----
        scoring = lab.get("scoring")
        score = None
        if scoring:
            compile_state = get_compile_status(student_folder, lab)
            if compile_state == "passed" and "compile_success_score" in scoring:
                score = scoring["compile_success_score"]
            elif "submit_score" in scoring:
                score = scoring["submit_score"]

        if score is not None:
            try:
                canvas_api_request(
                    f"courses/{COURSE_ID}/assignments/{assignment_id}"
                    f"/submissions/{student_id}",
                    method="PUT",
                    data={
                        "submission": {
                            "posted_grade": score,
                        }
                    },
                )
                logging.info(
                    "Posted score %s for student %s on assignment %s",
                    score, netid, assignment_id,
                )
            except Exception as e:
                logging.error("Failed to post score for %s: %s", netid, e)
                # Don't fail the submission just because grading failed

        logging.info(
            "Student %s (%s) submitted assignment %s (mode=%s, file_id=%s)",
            netid, student_id, assignment_id,
            "upload" if SUBMIT_AS_UPLOAD else "comment",
            file_id,
        )

        msg = "Submitted successfully to Canvas!"
        if score is not None:
            msg += f" Score: {score}"
        return jsonify(success=True, message=msg)

    except requests.exceptions.HTTPError as e:
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
# ROUTES – COMPILATION / DRC
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

    student_folder = get_submission_folder(session["student_id"], assignment_id)
    build_dir = prepare_build_directory(student_folder, lab)

    job_id = compile_queue.submit_job(
        student_id=session["student_id"],
        student_name=session["student_name"],
        netid=session.get("netid", ""),
        assignment_id=assignment_id,
        assignment_name=assignment_data["name"],
        lab_config=json.dumps(lab),
        lab_name=lab["template_dir"],
        build_dir=build_dir,
        student_folder=student_folder,  # NEW: needed for PCB result copy-back
    )

    return jsonify(success=True, job_id=job_id)

@app.route("/compile-status/<job_id>")
def compile_status(job_id):
    status = compile_queue.get_job_status(job_id)
    if not status:
        return jsonify(error="Not found"), 404

    # When compilation finishes, persist the result for the submit UI
    if status.get("status") in ("complete", "failed") and "student_id" in session:
        result = status.get("result", {})
        compile_success = bool(result.get("success"))
        job_meta = compile_queue.redis.hgetall(f"job:{job_id}")
        if job_meta:
            aid = job_meta.get("assignment_id", "")
            lab = get_lab_config_by_assignment_id(aid)
            if lab:
                sf = get_submission_folder(session["student_id"], aid)
                save_compile_status(sf, lab, compile_success)

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
        submit_mode="online_upload" if SUBMIT_AS_UPLOAD else "comment_attachment",
    )

# =============================================================================
# MAIN
# =============================================================================

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=False)
