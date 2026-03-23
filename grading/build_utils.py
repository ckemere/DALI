"""
Shared build utilities for grading embedded C labs.

Handles: zip extraction, infrastructure file setup, compilation,
flashing via DSLite, and video recording via ffmpeg.
"""

import os
import platform
import shutil
import subprocess
import time
import zipfile

from makefile_generator import (
    create_makefile_for_lab,
    verify_toolchain,
    DEVICE_NAME,
)

SCRIPT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_CCXML = os.path.join(SCRIPT_DIR, "MSPM0G3507.ccxml")
VIDEO_DURATION = 150  # seconds to record after flashing

# Files from the template that are needed for building but students
# don't modify (infrastructure files).
INFRASTRUCTURE_FILES = [
    "startup_mspm0g350x_ticlang.c",
    f"{DEVICE_NAME.lower()}.cmd",
]


def find_dslite():
    """Locate the DSLite binary from env var or PATH."""
    path = os.environ.get("DSLITE_PATH")
    if path and os.path.isfile(path):
        return path
    return shutil.which("DSLite")


def template_dir_for_lab(lab_name):
    """Return the template directory for a given lab (e.g. 'lab1')."""
    return os.path.join(SCRIPT_DIR, "template_files", lab_name)


def extract_submission(zip_path, build_dir):
    """
    Extract a submission zip into build_dir.
    Returns list of extracted filenames.
    """
    with zipfile.ZipFile(zip_path, "r") as zf:
        zf.extractall(build_dir)
        return zf.namelist()


def ensure_infrastructure(build_dir, lab_name):
    """
    Copy infrastructure files (startup, linker script) from template
    if they are not present in the student submission.
    """
    tpl_dir = template_dir_for_lab(lab_name)
    for fname in INFRASTRUCTURE_FILES:
        dest = os.path.join(build_dir, fname)
        if not os.path.isfile(dest):
            src = os.path.join(tpl_dir, fname)
            if os.path.isfile(src):
                shutil.copy2(src, dest)
            else:
                return False, f"Missing infrastructure file: {fname}"
    return True, ""


def compile_submission(build_dir, output_name):
    """
    Generate a Makefile and compile.

    Args:
        build_dir:    Directory with source files.
        output_name:  Base name for the output binary (e.g. "Lab_1").

    Returns (success, stdout, stderr).
    """
    source_files = [f for f in os.listdir(build_dir) if f.endswith(".c")]
    if not source_files:
        return False, "", "No .c files found in submission"

    create_makefile_for_lab(build_dir, source_files, output_name)

    proc = subprocess.run(
        ["make", "-C", build_dir, "all"],
        capture_output=True,
        text=True,
        timeout=60,
        env={**os.environ, "PATH": os.environ.get("PATH", "/usr/bin")},
    )
    return proc.returncode == 0, proc.stdout, proc.stderr


def flash_firmware(build_dir, dslite_path, ccxml_path, output_name):
    """
    Flash the compiled .out file onto the board using DSLite.

    Args:
        build_dir:    Directory containing the compiled .out file.
        dslite_path:  Path to the DSLite binary.
        ccxml_path:   Path to the CCXML target config.
        output_name:  Base name of the output binary (e.g. "Lab_1").

    Returns (success, stdout, stderr).
    """
    out_file = os.path.join(build_dir, f"{output_name}.out")
    if not os.path.isfile(out_file):
        return False, "", f"{output_name}.out not found after compilation"

    ccxml_abs = os.path.abspath(ccxml_path)

    proc = subprocess.run(
        [dslite_path, "flash", "--config", ccxml_abs, "-f", out_file],
        capture_output=True,
        text=True,
        timeout=30,
    )
    return proc.returncode == 0, proc.stdout, proc.stderr


def start_recording(output_path, duration=VIDEO_DURATION, camera_device=0,
                    settle_time=3):
    """
    Start recording video in the background using ffmpeg.
    Auto-detects platform (Linux v4l2, macOS avfoundation).
    Uses CRF 28 for good compression of mostly-static LED footage.

    Waits *settle_time* seconds after launching ffmpeg so the camera
    is actually capturing before the caller proceeds (e.g. to flash
    firmware).  This ensures the debug-LED flicker during programming
    is recorded.

    Returns a Popen process, or None on error.
    """
    system = platform.system()
    if system == "Linux":
        input_args = ["-f", "v4l2", "-framerate", "30",
                      "-i", f"/dev/video{camera_device}"]
    elif system == "Darwin":
        input_args = ["-f", "avfoundation", "-framerate", "30",
                      "-i", str(camera_device)]
    else:
        print(f"  Warning: unsupported platform {system} for recording")
        return None

    cmd = [
        "ffmpeg",
        "-y",
        *input_args,
        "-t", str(duration),
        "-c:v", "libx264",
        "-crf", "28",
        "-preset", "fast",
        "-pix_fmt", "yuv420p",
        output_path,
    ]
    try:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        # Give ffmpeg time to open the camera and start capturing
        # so the caller can be confident frames are being recorded.
        if settle_time > 0:
            time.sleep(settle_time)
            if proc.poll() is not None:
                # ffmpeg exited immediately — camera issue
                return None
        return proc
    except FileNotFoundError:
        return None


def finish_recording(proc, duration=VIDEO_DURATION):
    """
    Wait for a background ffmpeg recording to finish.
    Returns (success, error_message).
    """
    if proc is None:
        return False, "ffmpeg not found in PATH"
    try:
        _, stderr = proc.communicate(timeout=duration + 15)
        if proc.returncode == 0:
            return True, ""
        err_lines = stderr.decode(errors="replace").strip().split("\n")
        return False, err_lines[-1] if err_lines else "unknown error"
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()
        return False, "Video recording timed out"


def student_name_from_zip(zip_name):
    """
    Extract a student identifier from the zip filename.
    Strips common lab prefixes.
    """
    base = os.path.splitext(zip_name)[0]
    # Strip common lab prefixes
    for prefix in ("Lab_1_", "Lab 1_", "lab1_", "lab_1_",
                   "Lab_2_", "Lab 2_", "lab2_", "lab_2_",
                   "Lab_3_", "Lab 3_", "lab3_", "lab_3_"):
        if base.startswith(prefix):
            return base[len(prefix):]
    return base
