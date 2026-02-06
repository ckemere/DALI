import os
import io
import json
import zipfile
import shutil
import logging
from datetime import datetime
from flask import Flask, request, render_template, jsonify, session, redirect, url_for, flash
from werkzeug.utils import secure_filename
import requests

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
    "lab3": {
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
    }
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
    path = os.path.join(UPLOAD_FOLDER, f"student_{student_id}", f"assignment_{assignment_id}")
    os.makedirs(path, exist_ok=True)
    return path

def get_template_file_path(lab_name, filename):
    return os.path.join(TEMPLATE_FOLDER, lab_name, filename)

def get_lab_config(assignment_name):
    key = assignment_name.lower().replace(" ", "")
    return LAB_CONFIGS.get(key)

# =============================================================================
# CANVAS API
# =============================================================================

def canvas_api_request(endpoint, method="GET", data=None):
    url = f"{CANVAS_BASE_URL}/api/v1/{endpoint}"
    headers = {"Authorization": f"Bearer {CANVAS_API_TOKEN}"}

    if method == "GET":
        r = requests.get(url, headers=headers, timeout=30)
    elif method == "POST":
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
        sid = request.form.get("student_id")
        name = request.form.get("student_name")
        if sid and name:
            session["student_id"] = sid
            session["student_name"] = name
            return redirect(url_for("home"))
        flash("Student ID and name required")
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
    return render_template("home_api.html", assignments=assignments)

@app.route("/assignment/<assignment_id>")
def assignment(assignment_id):
    if "student_id" not in session:
        return redirect(url_for("login"))

    assignment_data = canvas_api_request(f"courses/{COURSE_ID}/assignments/{assignment_id}")
    lab = get_lab_config(assignment_data["name"])

    if not lab:
        flash("No lab configuration found")
        return redirect(url_for("home"))

    folder = get_submission_folder(session["student_id"], assignment_id)
    files = {f: os.path.exists(os.path.join(folder, f)) for f in lab["code_files"]}

    return render_template(
        "assignment_api.html",
        assignment=assignment_data,
        files=files,
        compile_available=compile_queue.is_available(),
    )

# =============================================================================
# ROUTES – COMPILATION
# =============================================================================

@app.route("/compile/<assignment_id>", methods=["POST"])
def compile_start(assignment_id):
    if "student_id" not in session:
        return jsonify(error="Not authenticated"), 403
    if not compile_queue.is_available():
        return jsonify(error="Compilation unavailable"), 503

    assignment = canvas_api_request(f"courses/{COURSE_ID}/assignments/{assignment_id}")
    lab = get_lab_config(assignment["name"])

    job_id = compile_queue.submit_job(
        student_id=session["student_id"],
        student_name=session["student_name"],
        assignment_id=assignment_id,
        assignment_name=assignment["name"],
        lab_config=lab,
        lab_name=assignment["name"].lower().replace(" ", ""),
    )

    return jsonify(success=True, job_id=job_id)

@app.route("/compile-status/<job_id>")
def compile_status(job_id):
    status = compile_queue.get_job_status(job_id)
    if not status:
        return jsonify(error="Not found"), 404
    return jsonify(status)

# =============================================================================
# MAIN
# =============================================================================

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=False)
