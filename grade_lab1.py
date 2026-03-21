#!/usr/bin/env python3
"""
Lab 1 Grading Script for DALI

Iterates through student submission zips, compiles each one using the
TI ARM Clang toolchain, and (if compilation succeeds) flashes the
resulting firmware onto a connected MSPM0G3507 LaunchPad via DSLite.

Usage:
    python grade_lab1.py --submissions-dir ./submissions --ccxml board.ccxml

Environment variables:
    TI_COMPILER_ROOT  - Path to TI ARM Clang compiler
    TI_SDK_ROOT       - Path to MSPM0 SDK
    DSLITE_PATH       - Path to DSLite binary
"""

import argparse
import csv
import os
import shutil
import subprocess
import sys
import tempfile
import zipfile
from datetime import datetime

from makefile_generator import (
    create_makefile_for_lab,
    verify_toolchain,
    DEVICE_NAME,
)

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
TEMPLATE_DIR = os.path.join(SCRIPT_DIR, "template_files", "lab1")
DEFAULT_CCXML = os.path.join(SCRIPT_DIR, "MSPM0G3507.ccxml")

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
    # Try PATH
    result = shutil.which("DSLite")
    if result:
        return result
    return None


def extract_submission(zip_path, build_dir):
    """
    Extract a submission zip into build_dir.
    Returns list of extracted filenames.
    """
    with zipfile.ZipFile(zip_path, "r") as zf:
        zf.extractall(build_dir)
        return zf.namelist()


def ensure_infrastructure(build_dir):
    """
    Copy infrastructure files (startup, linker script) from template
    if they are not present in the student submission.
    """
    for fname in INFRASTRUCTURE_FILES:
        dest = os.path.join(build_dir, fname)
        if not os.path.isfile(dest):
            src = os.path.join(TEMPLATE_DIR, fname)
            if os.path.isfile(src):
                shutil.copy2(src, dest)
            else:
                return False, f"Missing infrastructure file: {fname}"
    return True, ""


def compile_submission(build_dir):
    """
    Generate a Makefile and compile.
    Returns (success, stdout, stderr).
    """
    source_files = [f for f in os.listdir(build_dir) if f.endswith(".c")]
    if not source_files:
        return False, "", "No .c files found in submission"

    create_makefile_for_lab(build_dir, source_files, "Lab_1")

    proc = subprocess.run(
        ["make", "-C", build_dir, "all"],
        capture_output=True,
        text=True,
        timeout=60,
        env={**os.environ, "PATH": os.environ.get("PATH", "/usr/bin")},
    )
    return proc.returncode == 0, proc.stdout, proc.stderr


def flash_firmware(build_dir, dslite_path, ccxml_path):
    """
    Flash the compiled .out file onto the board using DSLite.
    Returns (success, stdout, stderr).
    """
    out_file = os.path.join(build_dir, "Lab_1.out")
    if not os.path.isfile(out_file):
        return False, "", "Lab_1.out not found after compilation"

    ccxml_abs = os.path.abspath(ccxml_path)

    proc = subprocess.run(
        [dslite_path, "flash", "--config", ccxml_abs, "-f", out_file],
        capture_output=True,
        text=True,
        timeout=30,
    )
    return proc.returncode == 0, proc.stdout, proc.stderr


def student_name_from_zip(zip_name):
    """
    Extract a student identifier from the zip filename.
    Expected format: Lab_1_<netid>.zip
    """
    base = os.path.splitext(zip_name)[0]
    # Strip the lab prefix if present
    for prefix in ("Lab_1_", "Lab 1_", "lab1_", "lab_1_"):
        if base.startswith(prefix):
            return base[len(prefix):]
    return base


def grade_all(submissions_dir, ccxml_path, dslite_path, results_csv, flash=True):
    """
    Main grading loop: iterate over zips, compile, optionally flash.
    """
    zip_files = sorted(
        f for f in os.listdir(submissions_dir) if f.endswith(".zip")
    )

    if not zip_files:
        print(f"No .zip files found in {submissions_dir}")
        return

    print(f"Found {len(zip_files)} submissions\n")

    results = []

    for i, zip_name in enumerate(zip_files, 1):
        student = student_name_from_zip(zip_name)
        zip_path = os.path.join(submissions_dir, zip_name)
        print(f"[{i}/{len(zip_files)}] {student}")

        row = {
            "student": student,
            "zip_file": zip_name,
            "compile_success": False,
            "compile_errors": "",
            "flash_success": False,
            "flash_errors": "",
        }

        build_dir = tempfile.mkdtemp(prefix=f"grade_{student}_")

        try:
            # Extract
            try:
                extracted = extract_submission(zip_path, build_dir)
                print(f"  Extracted {len(extracted)} files")
            except zipfile.BadZipFile:
                row["compile_errors"] = "Bad zip file"
                print(f"  ERROR: Bad zip file")
                results.append(row)
                continue

            # Ensure infrastructure files
            ok, err = ensure_infrastructure(build_dir)
            if not ok:
                row["compile_errors"] = err
                print(f"  ERROR: {err}")
                results.append(row)
                continue

            # Compile
            try:
                success, stdout, stderr = compile_submission(build_dir)
                row["compile_success"] = success
                if success:
                    print(f"  Compile: PASS")
                else:
                    # Keep first few lines of error
                    error_lines = stderr.strip().split("\n")
                    brief = "\n".join(error_lines[:5])
                    row["compile_errors"] = brief
                    print(f"  Compile: FAIL")
                    print(f"    {error_lines[0] if error_lines else 'unknown error'}")
            except subprocess.TimeoutExpired:
                row["compile_errors"] = "Compilation timed out"
                print(f"  Compile: TIMEOUT")
                results.append(row)
                continue

            # Flash (only if compile succeeded and flash is enabled)
            if success and flash:
                if not dslite_path:
                    row["flash_errors"] = "DSLite not found"
                    print(f"  Flash: SKIPPED (DSLite not found)")
                else:
                    try:
                        f_ok, f_out, f_err = flash_firmware(
                            build_dir, dslite_path, ccxml_path
                        )
                        row["flash_success"] = f_ok
                        if f_ok:
                            print(f"  Flash: PASS")
                        else:
                            row["flash_errors"] = f_err.strip()[:200]
                            print(f"  Flash: FAIL")
                            print(f"    {f_err.strip().split(chr(10))[0]}")
                    except subprocess.TimeoutExpired:
                        row["flash_errors"] = "Flash timed out"
                        print(f"  Flash: TIMEOUT")

                # Pause after flashing so the grader can observe the board
                if row["flash_success"]:
                    input("  >> Press Enter to continue to next student...")

        finally:
            shutil.rmtree(build_dir, ignore_errors=True)

        results.append(row)
        print()

    # Write results CSV
    if results:
        fieldnames = [
            "student", "zip_file",
            "compile_success", "compile_errors",
            "flash_success", "flash_errors",
        ]
        with open(results_csv, "w", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(results)
        print(f"\nResults written to {results_csv}")

    # Summary
    compiled = sum(1 for r in results if r["compile_success"])
    flashed = sum(1 for r in results if r["flash_success"])
    print(f"\nSummary: {compiled}/{len(results)} compiled, {flashed}/{len(results)} flashed")


def main():
    parser = argparse.ArgumentParser(
        description="Lab 1 Grading Script - compile and flash student submissions"
    )
    parser.add_argument(
        "--submissions-dir", required=True,
        help="Directory containing student submission .zip files"
    )
    parser.add_argument(
        "--ccxml", default=DEFAULT_CCXML,
        help="Path to the .ccxml target configuration file for DSLite (default: MSPM0G3507.ccxml)"
    )
    parser.add_argument(
        "--results-csv",
        default=f"lab1_results_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv",
        help="Output CSV file for results (default: timestamped)"
    )
    parser.add_argument(
        "--compile-only", action="store_true",
        help="Only compile, do not flash"
    )

    args = parser.parse_args()

    # Validate submissions directory
    if not os.path.isdir(args.submissions_dir):
        print(f"Error: {args.submissions_dir} is not a directory")
        sys.exit(1)

    # Verify toolchain
    ok, msg = verify_toolchain()
    if not ok:
        print(f"Toolchain error: {msg}")
        sys.exit(1)
    print(f"Toolchain OK: {msg}")

    # Find DSLite (only needed if flashing)
    dslite_path = None
    if not args.compile_only:
        dslite_path = find_dslite()
        if not dslite_path:
            print("Warning: DSLite not found. Set DSLITE_PATH env var.")
            print("Continuing in compile-only mode.\n")
        else:
            print(f"DSLite: {dslite_path}")

        # Validate ccxml
        if dslite_path and not os.path.isfile(args.ccxml):
            print(f"Error: ccxml file not found: {args.ccxml}")
            sys.exit(1)

    print()
    grade_all(
        submissions_dir=args.submissions_dir,
        ccxml_path=args.ccxml,
        dslite_path=dslite_path,
        results_csv=args.results_csv,
        flash=not args.compile_only,
    )


if __name__ == "__main__":
    main()
