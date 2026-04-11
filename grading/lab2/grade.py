#!/usr/bin/env python3
"""
Lab 2 Grading Script for DALI.

Handles three firmware phases with separate submission directories:
  Phase 1 (lab2-1): Lab 1 recapitulation (busy-wait LED clock)
  Phase 2 (lab2-2): Timer interrupt + standby sleep
  Phase 3 (lab2-3): PWM-modulated LED brightness + writeup

Modes:
  --capture         Compile, flash, and record videos for all phases
  --analyze-videos  Analyze pre-recorded videos (no hardware needed)
  --code-review     LLM code review of all phases + writeup
  --grade-batch     Combined: video analysis + LLM code review

Usage:
    python -m grading.lab2.grade --capture \
        --phase1-dir ./phase1_submissions \
        --phase2-dir ./phase2_submissions \
        --phase3-dir ./phase3_submissions \
        --ccxml board.ccxml --calibration calibration.json
"""

import argparse
import csv
import json
import os
import random
import re
import shutil
import subprocess
import sys
import tempfile
import time
import zipfile
from datetime import datetime

from grading.build_utils import (
    extract_submission,
    ensure_infrastructure,
    compile_submission,
    flash_firmware,
    start_recording,
    finish_recording,
    find_dslite,
    student_name_from_zip,
    DEFAULT_CCXML,
    VIDEO_DURATION,
)

from grading.video_analyzer import VideoAnalyzer
from assess.lab1_score import score as lab1_score, SCORE_FIELDS as LAB1_SCORE_FIELDS
from assess.lab2_score import (
    score_phase1, score_phase2, score_phase3,
    PHASE1_VIDEO_RUBRIC_ITEMS,
    PHASE3_VIDEO_RUBRIC_ITEMS, PHASE3_SCORE_FIELDS,
    video_verdict,
)
from grading.lab2.code_review import (
    RUBRIC_ITEMS,
    review_submission as review_single,
    review_bulk,
    DEFAULT_MODEL,
)

# Lab names used by ensure_infrastructure to find template dirs.
PHASE_LAB_NAMES = {
    "phase1": "lab2-1",
    "phase2": "lab2-2",
    "phase3": "lab2-3",
}
PHASE_OUTPUT_NAMES = {
    "phase1": "Lab2_Phase1",
    "phase2": "Lab2_Phase2",
    "phase3": "Lab2_Phase3",
}

# Video extensions
_VIDEO_EXTS = {".mp4", ".avi", ".mov", ".mkv"}

# Default video durations per phase (seconds).
DEFAULT_PHASE_DURATION = {
    "phase1": 150,
    "phase2": 150,
    "phase3": 150,
}

# Default recording frame rates.  Phase 1 and 2 only need to capture
# the 1 Hz LED clock behavior, so 5 fps is plenty (well above the
# Nyquist rate of 2 Hz) and saves disk space and analysis time.
# Phase 3 ideally wants high FPS so individual PWM cycles are visible,
# but built-in webcams typically cap at 30 fps and the analyzer has a
# low-fps fallback that infers PWM from brightness reduction instead.
# Override with --phaseN-fps if your camera supports it (60+ for
# partial PWM cycle resolution, 120+ for FFT-based PWM frequency
# estimation).
DEFAULT_PHASE_FPS = {
    "phase1": 5,
    "phase2": 5,
    "phase3": 30,
}

# Default capture resolution.  640x480 keeps file sizes manageable,
# is supported at 30+ fps by virtually every webcam, and gives the
# LED detector plenty of pixels.  Override with --video-size or the
# DALI_FFMPEG_VIDEO_SIZE env var.
DEFAULT_VIDEO_SIZE = "640x480"


def _match_students_across_phases(phase_dirs):
    """
    Discover student zip files across phase directories and match them
    by student name.

    Args:
        phase_dirs: dict mapping phase name -> directory path.

    Returns:
        dict mapping student name -> {phase: zip_path}
    """
    students = {}
    for phase, pdir in phase_dirs.items():
        if not pdir or not os.path.isdir(pdir):
            continue
        for f in sorted(os.listdir(pdir)):
            if f.endswith(".zip"):
                name = student_name_from_zip(f)
                if name not in students:
                    students[name] = {}
                students[name][phase] = os.path.join(pdir, f)
    return students


def _parse_rate_limit(err_str):
    """Parse a Gemini 429 error and return retry delay in seconds."""
    retry_secs = None
    m = re.search(r"['\"]retryDelay['\"]:\s*['\"](\d+)s?['\"]", err_str)
    if m:
        retry_secs = float(m.group(1))
    else:
        m = re.search(r'retry\w*\s+in\s+([\d.]+)s', err_str, re.IGNORECASE)
        if m:
            retry_secs = float(m.group(1))

    print("  LLM:   rate limited")

    if retry_secs is None:
        retry_secs = 55.0
        print(f"         defaulting to {retry_secs:.0f}s wait")
    else:
        print(f"         API requested retry in {retry_secs:.0f}s, "
              f"waiting {retry_secs + 5:.0f}s...")
    return retry_secs


# =====================================================================
# Capture mode: compile, flash, and record videos
# =====================================================================

def capture_videos(phase_dirs, ccxml_path, video_dir,
                   calibration_path=None, camera_device=0,
                   phase_durations=None, phase_fps=None,
                   compile_only=False, results_csv=None,
                   keep_builds_root=None, video_size=None):
    """
    Compile, flash, and record videos for each student across all phases.

    Videos are saved as <video_dir>/<phase>/<student>.mp4.

    If ``keep_builds_root`` is set, build artifacts (including the .out
    file) are written to ``<keep_builds_root>/<student>/<phase>/`` and
    are not deleted after each student.  Otherwise builds use a
    temporary directory that is removed once the phase is recorded.

    ``video_size`` is forwarded to start_recording() as the requested
    capture resolution (e.g. ``"640x480"``).  Pass ``None`` or an empty
    string to let ffmpeg/avfoundation pick.
    """
    if phase_durations is None:
        phase_durations = dict(DEFAULT_PHASE_DURATION)
    if phase_fps is None:
        phase_fps = dict(DEFAULT_PHASE_FPS)

    dslite_path = None
    if not compile_only:
        dslite_path = find_dslite()
        if not dslite_path:
            print("Warning: DSLite not found. Continuing in compile-only mode.")
            compile_only = True

    students = _match_students_across_phases(phase_dirs)
    if not students:
        print("No submissions found.")
        return

    print(f"Found {len(students)} students across "
          f"{len(phase_dirs)} phases\n")

    results = []

    for i, (student, zips) in enumerate(sorted(students.items()), 1):
        print(f"[{i}/{len(students)}] {student}")

        for phase in ("phase1", "phase2", "phase3"):
            if phase not in zips:
                print(f"  {phase}: no submission")
                continue

            zip_path = zips[phase]
            lab_name = PHASE_LAB_NAMES[phase]
            output_name = PHASE_OUTPUT_NAMES[phase]

            row = {
                "student": student,
                "phase": phase,
                "zip_file": os.path.basename(zip_path),
                "compile_success": False,
                "compile_errors": "",
                "flash_success": False,
                "flash_errors": "",
                "build_dir": "",
                "video_file": "",
            }

            if keep_builds_root:
                build_dir = os.path.join(
                    keep_builds_root, student, phase)
                os.makedirs(build_dir, exist_ok=True)
                cleanup_build_dir = False
            else:
                build_dir = tempfile.mkdtemp(
                    prefix=f"grade_{student}_{phase}_")
                cleanup_build_dir = True
            row["build_dir"] = build_dir

            try:
                # Extract
                try:
                    extracted = extract_submission(zip_path, build_dir)
                    print(f"  {phase}: extracted {len(extracted)} files")
                except zipfile.BadZipFile:
                    row["compile_errors"] = "Bad zip file"
                    print(f"  {phase}: ERROR bad zip")
                    results.append(row)
                    continue

                # Infrastructure files
                ok, err = ensure_infrastructure(build_dir, lab_name)
                if not ok:
                    row["compile_errors"] = err
                    print(f"  {phase}: ERROR {err}")
                    results.append(row)
                    continue

                # Compile
                try:
                    success, stdout, stderr = compile_submission(
                        build_dir, output_name)
                    row["compile_success"] = success
                    if success:
                        print(f"  {phase}: compile PASS")
                    else:
                        error_lines = stderr.strip().split("\n")
                        row["compile_errors"] = "\n".join(error_lines[:5])
                        print(f"  {phase}: compile FAIL")
                        print(f"    {error_lines[0] if error_lines else '?'}")
                except subprocess.TimeoutExpired:
                    row["compile_errors"] = "Compilation timed out"
                    print(f"  {phase}: compile TIMEOUT")
                    results.append(row)
                    continue

                # Flash + record
                if success and not compile_only and dslite_path:
                    phase_video_dir = os.path.join(video_dir, phase)
                    os.makedirs(phase_video_dir, exist_ok=True)
                    video_file = f"{student}.mp4"
                    video_path = os.path.join(phase_video_dir, video_file)
                    duration = phase_durations.get(phase, 150)
                    fps = phase_fps.get(phase, 30)

                    size_note = f" {video_size}" if video_size else ""
                    print(f"  {phase}: recording {duration}s "
                          f"@ {fps}fps{size_note}...")
                    rec_proc = start_recording(
                        video_path, duration, camera_device,
                        framerate=fps,
                        video_size=video_size or None)

                    try:
                        f_ok, f_out, f_err = flash_firmware(
                            build_dir, dslite_path, ccxml_path, output_name)
                        row["flash_success"] = f_ok
                        if f_ok:
                            print(f"  {phase}: flash PASS")
                        else:
                            err_text = (f_err or f_out or "").strip()
                            error_lines = (
                                err_text.split("\n") if err_text else [])
                            row["flash_errors"] = "\n".join(error_lines[:5])
                            print(f"  {phase}: flash FAIL")
                            if error_lines:
                                print(f"    {error_lines[0]}")
                    except subprocess.TimeoutExpired:
                        row["flash_success"] = False
                        row["flash_errors"] = "Flash timed out"
                        print(f"  {phase}: flash TIMEOUT")

                    if rec_proc is not None:
                        v_ok, v_err = finish_recording(rec_proc, duration)
                        if v_ok:
                            row["video_file"] = video_file
                            print(f"  {phase}: video saved")
                        else:
                            print(f"  {phase}: video FAILED ({v_err})")

            finally:
                if cleanup_build_dir:
                    shutil.rmtree(build_dir, ignore_errors=True)

            results.append(row)
        print()

    # Write results CSV.
    if results and results_csv:
        fieldnames = ["student", "phase", "zip_file",
                      "compile_success", "compile_errors",
                      "flash_success", "flash_errors",
                      "build_dir", "video_file"]
        with open(results_csv, "w", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames,
                                    extrasaction="ignore")
            writer.writeheader()
            writer.writerows(results)
        print(f"Results written to {results_csv}")

    compiled = sum(1 for r in results if r["compile_success"])
    flashed = sum(1 for r in results if r["flash_success"])
    print(f"\nSummary: {compiled}/{len(results)} compiled, "
          f"{flashed}/{len(results)} flashed")


# =====================================================================
# Analyze mode: score pre-recorded videos
# =====================================================================

def analyze_videos(video_dir, calibration_path, results_json,
                   threshold_override=None):
    """
    Batch-analyze pre-recorded videos for all three phases.

    Expects video_dir to contain phase1/, phase2/, phase3/
    subdirectories with <student>.mp4 files.

    Phase 1 and 2 use standard Lab 1 scoring.
    Phase 3 uses high-FPS PWM analysis.
    """
    analyzer = VideoAnalyzer(calibration_path)
    if threshold_override is not None:
        analyzer.outer_threshold = threshold_override
        analyzer.inner_threshold = threshold_override
    print(f"Calibration: {calibration_path}")
    print(f"Thresholds: outer={analyzer.outer_threshold}  "
          f"inner={analyzer.inner_threshold}")

    all_results = {}  # student -> {phase1: scores, phase2: scores, ...}

    for phase in ("phase1", "phase2", "phase3"):
        phase_video_dir = os.path.join(video_dir, phase)
        if not os.path.isdir(phase_video_dir):
            print(f"\n{phase}: directory not found, skipping")
            continue

        video_files = sorted(
            f for f in os.listdir(phase_video_dir)
            if os.path.splitext(f)[1].lower() in _VIDEO_EXTS
        )
        if not video_files:
            print(f"\n{phase}: no videos found")
            continue

        print(f"\n-- {phase} ({len(video_files)} videos) --")

        for vi, vname in enumerate(video_files, 1):
            student = os.path.splitext(vname)[0]
            video_path = os.path.join(phase_video_dir, vname)
            print(f"  [{vi}/{len(video_files)}] {student}...", end="",
                  flush=True)

            if student not in all_results:
                all_results[student] = {}

            try:
                timeline = analyzer.extract_timeline(video_path)
                if phase == "phase3":
                    scores, changes, _, _ = score_phase3(timeline, analyzer)
                    print(f"  PWM={scores.get('pwm_detected', '?')}  "
                          f"flicker={scores.get('no_visible_flicker', '?')}")
                elif phase == "phase1":
                    scores, changes, _, _ = score_phase1(timeline)
                    print(f"  timing={scores.get('timing_1hz', '?')}  "
                          f"LEDs={scores.get('leds_activated', '?')}")
                else:
                    scores, changes, _, _ = score_phase2(timeline)
                    print(f"  timing={scores.get('timing_1hz', '?')}  "
                          f"LEDs={scores.get('leds_activated', '?')}")

                all_results[student][phase] = scores

                # Save change log.
                changes_path = video_path.replace(
                    os.path.splitext(video_path)[1],
                    "_changes.json")
                with open(changes_path, "w") as cf:
                    json.dump(changes, cf, indent=1)

            except Exception as e:
                print(f"  FAILED ({e})")
                all_results[student][phase] = {"error": str(e)}

    # Write combined results.
    with open(results_json, "w") as f:
        json.dump(all_results, f, indent=2)
    print(f"\nVideo results written to {results_json}")

    analyzed = sum(1 for s in all_results.values()
                   for p in s.values() if "error" not in p)
    total = sum(len(s) for s in all_results.values())
    print(f"Summary: {analyzed}/{total} phase-videos analyzed successfully")


# =====================================================================
# Code review mode: LLM review of all phases + writeup
# =====================================================================

def code_review_batch(phase_dirs, llm_output=None,
                      api_key=None, model=DEFAULT_MODEL,
                      verbose=False, bulk_runs=0):
    """
    Run LLM code review across all students and phases.

    For each student, sends all three phases' code plus the writeup
    to Gemini in a single request.
    """
    students = _match_students_across_phases(phase_dirs)
    if not students:
        print("No submissions found.")
        return {}

    students_with_code = sorted(students.keys())
    print(f"Found {len(students_with_code)} students for code review")

    if bulk_runs:
        print(f"LLM model: {model}  (bulk mode: {bulk_runs} run(s))\n")
    else:
        print(f"LLM model: {model}\n")

    llm_results = {}

    if bulk_runs:
        # -- Bulk mode --
        print(f"-- LLM bulk review ({len(students_with_code)} students, "
              f"{bulk_runs} run(s)) --")

        # Extract all zips into temp dirs.
        student_phase_dirs = {}
        temp_dirs = []
        for student in students_with_code:
            zips = students[student]
            phase_extracted = {}
            for phase in ("phase1", "phase2", "phase3"):
                if phase in zips:
                    build_dir = tempfile.mkdtemp(
                        prefix=f"review_{student}_{phase}_")
                    temp_dirs.append(build_dir)
                    extract_submission(zips[phase], build_dir)
                    phase_extracted[phase] = build_dir
            if phase_extracted:
                student_phase_dirs[student] = phase_extracted

        # Run N times with shuffled order.
        all_run_results = []
        for run_idx in range(1, bulk_runs + 1):
            shuffled = list(student_phase_dirs.items())
            random.shuffle(shuffled)
            shuffled_dirs = dict(shuffled)

            print(f"\n  Run {run_idx}/{bulk_runs}  "
                  f"({len(shuffled_dirs)} students)")
            print(f"  Sending to {model}...")

            attempt = 0
            while True:
                attempt += 1
                try:
                    t0 = time.time()
                    bulk_result = review_bulk(
                        shuffled_dirs, api_key=api_key, model=model,
                        verbose=verbose)
                    dt = time.time() - t0
                    print(f"  Response received ({dt:.1f}s)")
                    break
                except Exception as e:
                    err_str = str(e)
                    is_rate_limit = ("429" in err_str
                                     or "RESOURCE_EXHAUSTED" in err_str)
                    if is_rate_limit:
                        retry_secs = _parse_rate_limit(err_str)
                        wait = retry_secs + 5
                        print(f"         (attempt {attempt})")
                        time.sleep(wait)
                    else:
                        raise

            for student in students_with_code:
                s_result = bulk_result.get(student, {})
                passes = sum(
                    1 for item_id in RUBRIC_ITEMS
                    if isinstance(s_result.get(item_id), dict)
                    and s_result[item_id].get("verdict") == "PASS"
                )
                print(f"    {student}: {passes}/{len(RUBRIC_ITEMS)} PASS")

            all_run_results.append(bulk_result)

        # Clean up.
        for d in temp_dirs:
            shutil.rmtree(d, ignore_errors=True)

        # Use first run; flag inconsistencies.
        print(f"\n-- Consistency check --")
        for student in students_with_code:
            first = all_run_results[0].get(student, {})
            llm_results[student] = first

            if bulk_runs > 1:
                inconsistent = []
                for item_id in RUBRIC_ITEMS:
                    verdicts = set()
                    for run_result in all_run_results:
                        s_res = run_result.get(student, {})
                        entry = s_res.get(item_id, {})
                        v = (entry.get("verdict", "MISSING")
                             if isinstance(entry, dict) else "UNCLEAR")
                        verdicts.add(v)
                    if len(verdicts) > 1:
                        inconsistent.append(
                            f"{item_id}: {' vs '.join(sorted(verdicts))}")

                if inconsistent:
                    llm_results[student]["_inconsistencies"] = inconsistent
                    print(f"  {student}: {len(inconsistent)} inconsistent")
                    for desc in inconsistent:
                        print(f"    {desc}")
                else:
                    print(f"  {student}: consistent")

    else:
        # -- Per-student mode --
        print("-- LLM per-student review --")
        for i, student in enumerate(students_with_code, 1):
            zips = students[student]
            print(f"\n  [{i}/{len(students_with_code)}] {student}: "
                  f"sending to {model}...")

            phase_extracted = {}
            temp_dirs_local = []
            try:
                for phase in ("phase1", "phase2", "phase3"):
                    if phase in zips:
                        build_dir = tempfile.mkdtemp(
                            prefix=f"review_{student}_{phase}_")
                        temp_dirs_local.append(build_dir)
                        extract_submission(zips[phase], build_dir)
                        phase_extracted[phase] = build_dir

                attempt = 0
                while True:
                    attempt += 1
                    try:
                        t0 = time.time()
                        result = review_single(
                            phase_extracted, api_key=api_key,
                            model=model, verbose=verbose)
                        break
                    except Exception as e:
                        err_str = str(e)
                        is_rate_limit = ("429" in err_str
                                         or "RESOURCE_EXHAUSTED" in err_str)
                        if is_rate_limit:
                            retry_secs = _parse_rate_limit(err_str)
                            wait = retry_secs + 5
                            print(f"         (attempt {attempt})")
                            time.sleep(wait)
                        else:
                            raise

                dt = time.time() - t0
                passes = sum(
                    1 for item_id in RUBRIC_ITEMS
                    if isinstance(result.get(item_id), dict)
                    and result[item_id].get("verdict") == "PASS"
                )
                llm_results[student] = result
                print(f"  {student}: {passes}/{len(RUBRIC_ITEMS)} PASS  "
                      f"({dt:.1f}s)")
            except Exception as e:
                print(f"  {student}: FAILED ({e})")
                llm_results[student] = {"_error": str(e)}
            finally:
                for d in temp_dirs_local:
                    shutil.rmtree(d, ignore_errors=True)

    if llm_output and llm_results:
        with open(llm_output, "w") as f:
            json.dump(llm_results, f, indent=2)
        print(f"\nLLM results written to {llm_output}")

    return llm_results


# =====================================================================
# Grade batch: combined video analysis + LLM review
# =====================================================================

def grade_batch(phase_dirs, video_dir, calibration_path,
                video_output=None, llm_output=None,
                api_key=None, model=DEFAULT_MODEL,
                threshold_override=None, verbose=False, bulk_runs=0,
                skip_video=False, skip_llm=False):
    """
    Combined batch: video analysis and/or LLM code review.

    This is the primary grading entry point when you already have
    recorded videos.
    """
    t_start = time.time()

    # -- Video analysis --
    video_results = {}
    if skip_video:
        print("-- Video analysis: SKIPPED --\n")
    elif video_dir and calibration_path:
        print("-- Video analysis --")
        analyzer = VideoAnalyzer(calibration_path)
        if threshold_override is not None:
            analyzer.outer_threshold = threshold_override
            analyzer.inner_threshold = threshold_override

        for phase in ("phase1", "phase2", "phase3"):
            phase_video_dir = os.path.join(video_dir, phase)
            if not os.path.isdir(phase_video_dir):
                continue

            video_files = sorted(
                f for f in os.listdir(phase_video_dir)
                if os.path.splitext(f)[1].lower() in _VIDEO_EXTS
            )
            print(f"\n  {phase}: {len(video_files)} videos")

            for vi, vname in enumerate(video_files, 1):
                student = os.path.splitext(vname)[0]
                video_path = os.path.join(phase_video_dir, vname)
                print(f"    [{vi}/{len(video_files)}] {student}...",
                      end="", flush=True)

                if student not in video_results:
                    video_results[student] = {}

                try:
                    timeline = analyzer.extract_timeline(video_path)
                    if phase == "phase3":
                        scores, changes, _, _ = score_phase3(
                            timeline, analyzer)
                    else:
                        scores, changes, _, _ = (
                            score_phase1(timeline) if phase == "phase1"
                            else score_phase2(timeline))

                    video_results[student][phase] = scores
                    print(f"  OK")

                    changes_path = video_path.replace(
                        os.path.splitext(video_path)[1],
                        "_changes.json")
                    with open(changes_path, "w") as cf:
                        json.dump(changes, cf, indent=1)

                except Exception as e:
                    print(f"  FAILED ({e})")
                    video_results[student][phase] = {"error": str(e)}

        dt_video = time.time() - t_start
        print(f"\nVideo phase done ({dt_video:.0f}s)")

    if video_output and video_results:
        with open(video_output, "w") as f:
            json.dump(video_results, f, indent=2)
        print(f"Video results written to {video_output}")

    # -- LLM code review --
    llm_results = {}
    if skip_llm:
        print("\n-- LLM code review: SKIPPED --\n")
    else:
        llm_results = code_review_batch(
            phase_dirs, llm_output=llm_output,
            api_key=api_key, model=model,
            verbose=verbose, bulk_runs=bulk_runs)

    total_time = time.time() - t_start
    print(f"\nDone: {len(video_results)} video + {len(llm_results)} LLM "
          f"({total_time:.0f}s total)")


# =====================================================================
# CLI
# =====================================================================

def main():
    parser = argparse.ArgumentParser(
        description="Lab 2 Grading - compile, flash, record, analyze, "
                    "and LLM review across three firmware phases")

    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument(
        "--capture", action="store_true",
        help="Compile, flash, and record videos for all phases")
    mode.add_argument(
        "--analyze-videos", metavar="VIDEO_DIR",
        help="Batch-analyze pre-recorded videos in "
             "VIDEO_DIR/{phase1,phase2,phase3}/")
    mode.add_argument(
        "--code-review", action="store_true",
        help="LLM code review only (no video)")
    mode.add_argument(
        "--grade-batch", action="store_true",
        help="Combined: video analysis + LLM code review")

    # Phase submission directories.
    parser.add_argument(
        "--phase1-dir",
        help="Directory of Phase 1 submission .zip files")
    parser.add_argument(
        "--phase2-dir",
        help="Directory of Phase 2 submission .zip files")
    parser.add_argument(
        "--phase3-dir",
        help="Directory of Phase 3 submission .zip files")

    # Compilation / flashing.
    parser.add_argument(
        "--ccxml", default=DEFAULT_CCXML,
        help="Path to .ccxml target configuration for DSLite")
    parser.add_argument(
        "--compile-only", action="store_true",
        help="Only compile, do not flash or record")
    parser.add_argument(
        "--keep-builds", metavar="DIR",
        help="If set, build artifacts (.out, .map, .o) for each "
             "student/phase are written to DIR/<student>/<phase>/ "
             "and not deleted after capture, so failures can be "
             "re-flashed by hand")

    # Video recording.
    parser.add_argument(
        "--video-dir", default="videos",
        help="Directory for recorded/pre-recorded videos "
             "(default: videos/)")
    parser.add_argument(
        "--camera", type=int, default=0,
        help="Camera device index (default: 0)")
    parser.add_argument(
        "--phase1-fps", type=int, default=5,
        help="Recording frame rate for Phase 1 (default: 5 — the "
             "1 Hz LED clock is well above this Nyquist limit)")
    parser.add_argument(
        "--phase2-fps", type=int, default=5,
        help="Recording frame rate for Phase 2 (default: 5)")
    parser.add_argument(
        "--phase3-fps", type=int, default=30,
        help="Recording frame rate for Phase 3 (default: 30 — works "
             "on built-in webcams; the analyzer has a low-fps fallback "
             "that infers PWM from brightness reduction.  Set to 120+ "
             "if your camera supports it for direct PWM cycle "
             "observation and FFT-based frequency estimation)")
    parser.add_argument(
        "--video-size", default=DEFAULT_VIDEO_SIZE,
        help=f"Capture resolution WxH (default: {DEFAULT_VIDEO_SIZE}). "
             "Pass an empty string to let ffmpeg/avfoundation pick.")
    parser.add_argument(
        "--phase3-duration", type=int, default=150,
        help="Recording duration for Phase 3 in seconds (default: 150)")

    # Video analysis.
    parser.add_argument(
        "--calibration",
        help="Path to calibration JSON (enables video analysis)")
    parser.add_argument(
        "--threshold", type=int, default=None,
        help="Override brightness threshold from calibration")

    # LLM code review.
    parser.add_argument(
        "--api-key",
        help="Gemini API key (default: GEMINI_API_KEY env var)")
    parser.add_argument(
        "--model", default=DEFAULT_MODEL,
        help=f"Gemini model (default: {DEFAULT_MODEL})")
    parser.add_argument(
        "--verbose-llm", action="store_true",
        help="Print LLM prompts and raw responses")
    parser.add_argument(
        "--bulk", type=int, default=0, metavar="N",
        help="Bulk LLM mode: N runs with shuffled order")
    parser.add_argument(
        "--skip-video", action="store_true",
        help="Skip video analysis (LLM only, for --grade-batch)")
    parser.add_argument(
        "--skip-llm", action="store_true",
        help="Skip LLM review (video only, for --grade-batch)")

    # Output.
    parser.add_argument(
        "--results-csv",
        default=f"lab2_results_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv",
        help="Output CSV for capture results")
    parser.add_argument(
        "--video-output", metavar="FILE",
        default="video_results.json",
        help="Output path for video analysis JSON")
    parser.add_argument(
        "--llm-output", metavar="FILE",
        default="llm_results.json",
        help="Output path for LLM review JSON")

    args = parser.parse_args()

    # Build phase_dirs from CLI args.
    phase_dirs = {}
    if args.phase1_dir:
        phase_dirs["phase1"] = args.phase1_dir
    if args.phase2_dir:
        phase_dirs["phase2"] = args.phase2_dir
    if args.phase3_dir:
        phase_dirs["phase3"] = args.phase3_dir

    # Build per-phase fps/duration overrides.
    phase_fps = dict(DEFAULT_PHASE_FPS)
    phase_fps["phase1"] = args.phase1_fps
    phase_fps["phase2"] = args.phase2_fps
    phase_fps["phase3"] = args.phase3_fps
    phase_durations = dict(DEFAULT_PHASE_DURATION)
    phase_durations["phase3"] = args.phase3_duration

    # -- Capture mode --
    if args.capture:
        if not phase_dirs:
            print("Error: provide at least one --phaseN-dir")
            sys.exit(1)
        os.makedirs(args.video_dir, exist_ok=True)
        keep_builds_root = (
            os.path.abspath(args.keep_builds)
            if args.keep_builds else None)
        if keep_builds_root:
            os.makedirs(keep_builds_root, exist_ok=True)
        capture_videos(
            phase_dirs=phase_dirs,
            ccxml_path=args.ccxml,
            video_dir=args.video_dir,
            calibration_path=args.calibration,
            camera_device=args.camera,
            phase_durations=phase_durations,
            phase_fps=phase_fps,
            compile_only=args.compile_only,
            results_csv=args.results_csv,
            keep_builds_root=keep_builds_root,
            video_size=args.video_size or None,
        )
        sys.exit(0)

    # -- Analyze-videos mode --
    if args.analyze_videos:
        if not args.calibration:
            print("Error: --calibration is required for --analyze-videos")
            sys.exit(1)
        analyze_videos(
            video_dir=args.analyze_videos,
            calibration_path=args.calibration,
            results_json=args.video_output,
            threshold_override=args.threshold,
        )
        sys.exit(0)

    # -- Code-review mode --
    if args.code_review:
        if not phase_dirs:
            print("Error: provide at least one --phaseN-dir")
            sys.exit(1)
        api_key = args.api_key or os.environ.get("GEMINI_API_KEY")
        if not api_key:
            print("Error: set GEMINI_API_KEY or pass --api-key")
            sys.exit(1)
        code_review_batch(
            phase_dirs=phase_dirs,
            llm_output=args.llm_output,
            api_key=api_key,
            model=args.model,
            verbose=args.verbose_llm,
            bulk_runs=args.bulk,
        )
        sys.exit(0)

    # -- Grade-batch mode --
    if args.grade_batch:
        if not phase_dirs:
            print("Error: provide at least one --phaseN-dir")
            sys.exit(1)
        api_key = args.api_key or os.environ.get("GEMINI_API_KEY")
        if not api_key and not args.skip_llm:
            print("Error: set GEMINI_API_KEY or pass --api-key")
            sys.exit(1)
        grade_batch(
            phase_dirs=phase_dirs,
            video_dir=args.video_dir,
            calibration_path=args.calibration,
            video_output=args.video_output,
            llm_output=args.llm_output,
            api_key=api_key,
            model=args.model,
            threshold_override=args.threshold,
            verbose=args.verbose_llm,
            bulk_runs=args.bulk,
            skip_video=args.skip_video,
            skip_llm=args.skip_llm,
        )
        sys.exit(0)


if __name__ == "__main__":
    main()
