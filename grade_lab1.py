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
import json
import os
import platform
import shutil
import subprocess
import sys
import tempfile
import time
import zipfile
from datetime import datetime

from makefile_generator import (
    create_makefile_for_lab,
    verify_toolchain,
    DEVICE_NAME,
)

try:
    from analyze_lab1_video import VideoAnalyzer, SCORE_FIELDS
except ImportError:
    VideoAnalyzer = None
    SCORE_FIELDS = []

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
TEMPLATE_DIR = os.path.join(SCRIPT_DIR, "template_files", "lab1")
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
    Expected format: Lab_1_<netid>.zip
    """
    base = os.path.splitext(zip_name)[0]
    # Strip the lab prefix if present
    for prefix in ("Lab_1_", "Lab 1_", "lab1_", "lab_1_"):
        if base.startswith(prefix):
            return base[len(prefix):]
    return base


def grade_all(submissions_dir, ccxml_path, dslite_path, results_csv,
              flash=True, video_dir=None, video_duration=VIDEO_DURATION,
              calibration_path=None, camera_device=0):
    """
    Main grading loop: iterate over zips, compile, optionally flash.
    If calibration_path is provided, runs video analysis after recording.
    """
    analyzer = None
    if calibration_path and VideoAnalyzer is not None:
        try:
            analyzer = VideoAnalyzer(calibration_path)
            print(f"Video analysis enabled (calibration: {calibration_path})")
        except Exception as e:
            print(f"Warning: could not load calibration: {e}")
            print("Video analysis disabled.\n")
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
            "video_file": "",
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
                    # Start recording before flashing so we capture
                    # the board from the moment it begins running.
                    rec_proc = None
                    video_file = None
                    if video_dir:
                        video_file = f"{student}.mp4"
                        video_path = os.path.join(video_dir, video_file)
                        print(f"  Recording {video_duration}s video...")
                        rec_proc = start_recording(video_path, video_duration,
                                                   camera_device)

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

                    # Wait for video recording to finish
                    if rec_proc is not None:
                        v_ok, v_err = finish_recording(rec_proc, video_duration)
                        if v_ok:
                            row["video_file"] = video_file
                            print(f"  Video: saved to {video_file}")

                            # Run video analysis if calibration available
                            if analyzer is not None:
                                try:
                                    print(f"  Analyzing video...")
                                    timeline = analyzer.extract_timeline(video_path)
                                    scores, changes, _, _ = analyzer.score(timeline)
                                    for k, v in scores.items():
                                        row[f"video_{k}"] = v
                                    # Save change log alongside video
                                    changes_path = os.path.join(
                                        video_dir, f"{student}_changes.json"
                                    )
                                    with open(changes_path, "w") as cf:
                                        json.dump(changes, cf, indent=1)
                                    print(f"  Analysis: {scores.get('leds_activated', '?')} LEDs, "
                                          f"timing={scores.get('timing_1hz', '?')}, "
                                          f"wrap={scores.get('sequence_wrap', '?')}")
                                except Exception as e:
                                    print(f"  Analysis: FAILED ({e})")
                                    row["video_analysis_error"] = str(e)
                        else:
                            print(f"  Video: FAILED ({v_err})")

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
            "video_file",
        ]
        # Add video analysis columns if any row has them
        if analyzer is not None:
            fieldnames.extend(f"video_{k}" for k in SCORE_FIELDS)
            fieldnames.append("video_analysis_error")
        with open(results_csv, "w", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames,
                                    extrasaction="ignore")
            writer.writeheader()
            writer.writerows(results)
        print(f"\nResults written to {results_csv}")

    # Summary
    compiled = sum(1 for r in results if r["compile_success"])
    flashed = sum(1 for r in results if r["flash_success"])
    print(f"\nSummary: {compiled}/{len(results)} compiled, {flashed}/{len(results)} flashed")


def grade_single_zip(zip_path, ccxml_path=DEFAULT_CCXML, calibration_path=None,
                     camera_device=0, video_duration=VIDEO_DURATION,
                     compile_only=False, keep_build=False,
                     video_dir=None, existing_video=None,
                     threshold_override=None):
    """
    Process one student zip end-to-end with verbose output at every step.
    Useful for debugging the pipeline before running the full batch.

    Args:
        zip_path:          Path to the student .zip
        ccxml_path:        CCXML board config for DSLite
        calibration_path:  Calibration JSON (enables video analysis)
        camera_device:     Camera index for ffmpeg
        video_duration:    Seconds to record
        compile_only:      Stop after compilation
        keep_build:        Keep the build directory on disk for inspection
        video_dir:         Directory to save recorded videos
        existing_video:    Path to a pre-recorded video (skip flash+record)

    Returns:
        dict with results from each pipeline stage
    """
    result = {"zip": zip_path, "student": student_name_from_zip(os.path.basename(zip_path))}
    student = result["student"]
    print(f"=== Single-zip mode: {student} ===\n")

    # ── 1. Toolchain ──────────────────────────────────────────────
    print("Step 1/5: Verify toolchain")
    ok, msg = verify_toolchain()
    if not ok:
        print(f"  FAIL: {msg}")
        result["toolchain"] = msg
        return result
    print(f"  OK: {msg}")
    result["toolchain"] = "ok"

    dslite_path = None
    if not compile_only:
        dslite_path = find_dslite()
        print(f"  DSLite: {dslite_path or 'NOT FOUND'}")

    # ── 2. Extract ────────────────────────────────────────────────
    print("\nStep 2/5: Extract zip")
    build_dir = tempfile.mkdtemp(prefix=f"grade_{student}_")
    print(f"  Build dir: {build_dir}")
    try:
        extracted = extract_submission(zip_path, build_dir)
        print(f"  Extracted {len(extracted)} files:")
        for f in sorted(extracted):
            print(f"    {f}")
    except zipfile.BadZipFile:
        print("  FAIL: Bad zip file")
        result["extract"] = "bad_zip"
        if not keep_build:
            shutil.rmtree(build_dir, ignore_errors=True)
        return result

    ok, err = ensure_infrastructure(build_dir)
    if not ok:
        print(f"  FAIL: {err}")
        result["extract"] = err
        if not keep_build:
            shutil.rmtree(build_dir, ignore_errors=True)
        return result
    print("  Infrastructure files: OK")
    result["extract"] = "ok"
    result["build_dir"] = build_dir

    # ── 3. Compile ────────────────────────────────────────────────
    print("\nStep 3/5: Compile")
    try:
        success, stdout, stderr = compile_submission(build_dir)
    except subprocess.TimeoutExpired:
        print("  FAIL: Compilation timed out (60 s)")
        result["compile"] = "timeout"
        if not keep_build:
            shutil.rmtree(build_dir, ignore_errors=True)
        return result

    result["compile"] = "ok" if success else "fail"
    if success:
        out_file = os.path.join(build_dir, "Lab_1.out")
        size = os.path.getsize(out_file) if os.path.isfile(out_file) else 0
        print(f"  PASS  (Lab_1.out = {size:,} bytes)")
    else:
        print(f"  FAIL")
        print(f"  --- stderr ---")
        for line in stderr.strip().split("\n")[:20]:
            print(f"  {line}")
        print(f"  --- end ---")
        if not keep_build:
            shutil.rmtree(build_dir, ignore_errors=True)
        return result

    if compile_only and not existing_video:
        print(f"\n  --compile-only: stopping here.")
        print(f"  Build dir: {build_dir}")
        return result

    # ── 4. Flash + Record ─────────────────────────────────────────
    if existing_video:
        # Skip flash/record entirely — use the provided video
        print("\nStep 4/5: Flash firmware")
        print(f"  SKIP: using existing video {existing_video}")
        result["flash"] = "skipped"
        result["video"] = existing_video
    else:
        print("\nStep 4/5: Flash firmware")
        if not dslite_path:
            print("  SKIP: DSLite not found (set DSLITE_PATH)")
            result["flash"] = "skipped"
            if not keep_build:
                shutil.rmtree(build_dir, ignore_errors=True)
            return result

        # Decide where to save the video
        if video_dir is None:
            video_dir = os.path.dirname(os.path.abspath(zip_path))
        os.makedirs(video_dir, exist_ok=True)

        # Start recording BEFORE flashing so we capture the debug LED
        video_path = os.path.join(video_dir, f"{student}.mp4")
        print(f"  Starting {video_duration}s recording -> {video_path}")
        rec_proc = start_recording(video_path, video_duration, camera_device)

        try:
            f_ok, f_out, f_err = flash_firmware(build_dir, dslite_path, ccxml_path)
        except subprocess.TimeoutExpired:
            f_ok, f_err = False, "Flash timed out"

        result["flash"] = "ok" if f_ok else "fail"
        if f_ok:
            print(f"  PASS")
        else:
            print(f"  FAIL: {f_err.strip().split(chr(10))[0]}")

        # Wait for recording
        if rec_proc is not None:
            print(f"  Waiting for video ({video_duration}s)...")
            v_ok, v_err = finish_recording(rec_proc, video_duration)
            if v_ok:
                fsize = os.path.getsize(video_path)
                print(f"  Video saved: {video_path} ({fsize:,} bytes)")
                result["video"] = video_path
            else:
                print(f"  Video FAILED: {v_err}")
                result["video"] = None

    if not keep_build:
        shutil.rmtree(build_dir, ignore_errors=True)

    # ── 5. Analyze ────────────────────────────────────────────────
    print("\nStep 5/5: Video analysis")
    if not result.get("video"):
        print("  SKIP: no video recorded")
        result["analysis"] = "skipped"
        return result

    if not calibration_path:
        print("  SKIP: no --calibration provided")
        result["analysis"] = "skipped"
        return result

    if VideoAnalyzer is None:
        print("  SKIP: opencv-python not installed")
        result["analysis"] = "skipped"
        return result

    try:
        analyzer = VideoAnalyzer(calibration_path)
        if threshold_override is not None:
            analyzer.outer_threshold = threshold_override
            analyzer.inner_threshold = threshold_override
        timeline = analyzer.extract_timeline(result["video"], verbose=True)
        scores, changes, _, _ = analyzer.score(timeline)
        result["scores"] = scores
        result["analysis"] = "ok"

        print(f"  t0 offset:   {scores.get('t0_offset', '?')}")
        print(f"  LEDs off:    {scores.get('leds_start_off', '?')}")
        print(f"  Activated:   {scores.get('leds_activated', '?')}")
        print(f"  Avg on:      {scores.get('avg_leds_on', '?')}")
        print(f"  Two hands:   {scores.get('two_hands', '?')}")
        print(f"  Timing:      {scores.get('timing_1hz', '?')} "
              f"({scores.get('timing_interval', '')})")
        print(f"  Loop:        {scores.get('infinite_loop', '?')}")
        print(f"  Wrap:        {scores.get('sequence_wrap', '?')}")
        print(f"  Changes:     {scores.get('total_state_changes', '?')}")

        # Save change log next to video
        changes_path = result["video"].replace(".mp4", "_changes.json")
        with open(changes_path, "w") as cf:
            json.dump(changes, cf, indent=1)
        print(f"\n  Change log: {changes_path}")
    except Exception as e:
        print(f"  FAILED: {e}")
        result["analysis"] = str(e)

    print(f"\n=== Done: {student} ===")
    return result


def main():
    parser = argparse.ArgumentParser(
        description="Lab 1 Grading Script - compile and flash student submissions"
    )

    # Single-zip mode vs batch mode
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument(
        "--zip",
        help="Process a single student .zip (debug mode)"
    )
    source.add_argument(
        "--submissions-dir",
        help="Directory containing student submission .zip files (batch mode)"
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
    parser.add_argument(
        "--video-duration", type=int, default=VIDEO_DURATION,
        help=f"Seconds of video to record after each flash (default: {VIDEO_DURATION})"
    )
    parser.add_argument(
        "--calibration",
        help="Path to calibration JSON from calibrate_lab1.py (enables video analysis)"
    )
    parser.add_argument(
        "--camera", type=int, default=0,
        help="Camera device index for recording (default: 0)"
    )
    parser.add_argument(
        "--keep-build", action="store_true",
        help="Keep temporary build directory (for --zip debugging)"
    )
    parser.add_argument(
        "--video-dir",
        help="Directory to save recorded videos (default: next to zip/results)"
    )
    parser.add_argument(
        "--video",
        help="Path to a pre-recorded video (skip flash+record, run analysis only; --zip mode)"
    )
    parser.add_argument(
        "--threshold", type=int, default=None,
        help="Override brightness threshold from calibration (e.g. 180)"
    )

    args = parser.parse_args()

    # ── Single-zip mode ───────────────────────────────────────────
    if args.zip:
        if not os.path.isfile(args.zip):
            print(f"Error: {args.zip} not found")
            sys.exit(1)
        if args.video and not os.path.isfile(args.video):
            print(f"Error: {args.video} not found")
            sys.exit(1)
        result = grade_single_zip(
            zip_path=args.zip,
            ccxml_path=args.ccxml,
            calibration_path=args.calibration,
            camera_device=args.camera,
            video_duration=args.video_duration,
            compile_only=args.compile_only,
            keep_build=args.keep_build,
            video_dir=args.video_dir,
            existing_video=args.video,
            threshold_override=args.threshold,
        )
        # Print summary as JSON for easy inspection
        print("\n--- Result summary ---")
        summary = {k: v for k, v in result.items() if k != "scores"}
        print(json.dumps(summary, indent=2))
        if "scores" in result:
            print("\n--- Scores ---")
            print(json.dumps(result["scores"], indent=2))
        sys.exit(0 if result.get("compile") == "ok" else 1)

    # ── Batch mode ────────────────────────────────────────────────
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

    # Set up video directory next to the results CSV
    video_dir = None
    if not args.compile_only and dslite_path:
        if args.video_dir:
            video_dir = args.video_dir
        else:
            results_parent = os.path.dirname(os.path.abspath(args.results_csv))
            video_dir = os.path.join(results_parent, "videos")
        os.makedirs(video_dir, exist_ok=True)
        print(f"Videos: {video_dir} ({args.video_duration}s each)")
        # Verify ffmpeg is available
        if not shutil.which("ffmpeg"):
            print("Warning: ffmpeg not found in PATH. Video recording will fail.")

    # Validate calibration file if provided
    if args.calibration:
        if not os.path.isfile(args.calibration):
            print(f"Error: calibration file not found: {args.calibration}")
            sys.exit(1)
        if VideoAnalyzer is None:
            print("Warning: opencv-python not installed. Video analysis disabled.")
            print("  pip install opencv-python numpy\n")

    print()
    grade_all(
        submissions_dir=args.submissions_dir,
        ccxml_path=args.ccxml,
        dslite_path=dslite_path,
        results_csv=args.results_csv,
        flash=not args.compile_only,
        video_dir=video_dir,
        video_duration=args.video_duration,
        calibration_path=args.calibration,
        camera_device=args.camera,
    )


if __name__ == "__main__":
    main()
