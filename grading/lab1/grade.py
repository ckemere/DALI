#!/usr/bin/env python3
"""
Lab 1 Grading Script for DALI.

Iterates through student submission zips, compiles each one using the
TI ARM Clang toolchain, and (if compilation succeeds) flashes the
resulting firmware onto a connected MSPM0G3507 LaunchPad via DSLite.

Usage:
    python -m grading.lab1.grade --submissions-dir ./submissions --ccxml board.ccxml
"""

import argparse
import csv
import json
import os
import shutil
import subprocess
import sys
import tempfile
import zipfile
from datetime import datetime

from makefile_generator import verify_toolchain

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
from grading.lab1.score import score, SCORE_FIELDS
from grading.lab1.code_review import (
    RUBRIC_ITEMS,
    review_submission,
    DEFAULT_MODEL,
)

LAB_NAME = "lab1"
OUTPUT_NAME = "Lab_1"

# Video extensions to look for in --analyze-dir mode
_VIDEO_EXTS = {".mp4", ".avi", ".mov", ".mkv"}


def analyze_videos(video_dir, calibration_path, results_csv,
                   threshold_override=None):
    """
    Batch-analyze pre-recorded videos in a directory.

    Expects video files named <student>.mp4 (the stem becomes the
    student identifier).  Produces a CSV with one row per video.
    """
    analyzer = VideoAnalyzer(calibration_path)
    if threshold_override is not None:
        analyzer.outer_threshold = threshold_override
        analyzer.inner_threshold = threshold_override
    print(f"Calibration: {calibration_path}")
    print(f"Thresholds: outer={analyzer.outer_threshold}  "
          f"inner={analyzer.inner_threshold}  debug={analyzer.debug_threshold}")

    video_files = sorted(
        f for f in os.listdir(video_dir)
        if os.path.splitext(f)[1].lower() in _VIDEO_EXTS
    )
    if not video_files:
        print(f"No video files found in {video_dir}")
        return

    print(f"Found {len(video_files)} videos\n")

    results = []
    for i, vname in enumerate(video_files, 1):
        student = os.path.splitext(vname)[0]
        video_path = os.path.join(video_dir, vname)
        print(f"[{i}/{len(video_files)}] {student}")

        row = {"student": student, "video_file": vname}

        try:
            timeline = analyzer.extract_timeline(video_path)
            scores, changes, _, _ = score(timeline)
            for k, v in scores.items():
                row[k] = v

            # Save change log alongside video
            changes_path = os.path.join(
                video_dir, f"{student}_changes.json"
            )
            with open(changes_path, "w") as cf:
                json.dump(changes, cf, indent=1)

            print(f"  LEDs: {scores.get('leds_activated', '?')}  "
                  f"timing={scores.get('timing_1hz', '?')}  "
                  f"inner_cw={scores.get('inner_clockwise_sequence', '?')}  "
                  f"outer_cw={scores.get('outer_clockwise_sequence', '?')}")
        except Exception as e:
            print(f"  FAILED: {e}")
            row["analysis_error"] = str(e)

        results.append(row)

    # Write CSV
    if results:
        fieldnames = ["student", "video_file"]
        fieldnames.extend(SCORE_FIELDS)
        fieldnames.append("analysis_error")
        with open(results_csv, "w", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames,
                                    extrasaction="ignore")
            writer.writeheader()
            writer.writerows(results)
        print(f"\nResults written to {results_csv}")

    analyzed = sum(1 for r in results if "analysis_error" not in r)
    print(f"\nSummary: {analyzed}/{len(results)} analyzed successfully")


def grade_batch(submissions_dir, video_dir, calibration_path, results_csv,
                api_key=None, model=DEFAULT_MODEL,
                threshold_override=None, verbose=False):
    """
    Combined batch grading: LLM code review of zips + video analysis of
    pre-recorded videos, merged into a single CSV.

    Matches students by name: zip files are matched to video files using
    student_name_from_zip() for zips and the video filename stem.

    Args:
        submissions_dir: Directory of student .zip files.
        video_dir:       Directory of pre-recorded <student>.mp4 videos.
        calibration_path: Path to calibration JSON for video analysis.
        results_csv:     Output CSV path.
        api_key:         Gemini API key (or GEMINI_API_KEY env var).
        model:           Gemini model name.
        threshold_override: Optional brightness threshold override.
        verbose:         Print LLM prompts/responses.
    """
    import time

    # Discover zips
    zip_files = {}
    if submissions_dir and os.path.isdir(submissions_dir):
        for f in sorted(os.listdir(submissions_dir)):
            if f.endswith(".zip"):
                name = student_name_from_zip(f)
                zip_files[name] = os.path.join(submissions_dir, f)

    # Discover videos
    video_files = {}
    analyzer = None
    if video_dir and os.path.isdir(video_dir):
        if calibration_path:
            analyzer = VideoAnalyzer(calibration_path)
            if threshold_override is not None:
                analyzer.outer_threshold = threshold_override
                analyzer.inner_threshold = threshold_override
        for f in sorted(os.listdir(video_dir)):
            if os.path.splitext(f)[1].lower() in _VIDEO_EXTS:
                name = os.path.splitext(f)[0]
                video_files[name] = os.path.join(video_dir, f)

    # Union of all student names
    all_students = sorted(set(zip_files) | set(video_files))
    if not all_students:
        print("No submissions or videos found.")
        return

    print(f"Students: {len(all_students)} "
          f"({len(zip_files)} zips, {len(video_files)} videos)")
    if analyzer:
        print(f"Calibration: {calibration_path}")
        print(f"Thresholds: outer={analyzer.outer_threshold}  "
              f"inner={analyzer.inner_threshold}")
    print(f"LLM model: {model}\n")

    # ── Prepare CSV for incremental writes ─────────────────────
    fieldnames = ["student"]
    fieldnames.extend(f"video_{k}" for k in SCORE_FIELDS)
    fieldnames.append("video_error")
    for item_id in RUBRIC_ITEMS:
        fieldnames.append(f"llm_{item_id}_verdict")
        fieldnames.append(f"llm_{item_id}_reason")
        fieldnames.append(f"llm_{item_id}_evidence")
    fieldnames.append("llm_error")

    csv_file = open(results_csv, "w", newline="")
    writer = csv.DictWriter(csv_file, fieldnames=fieldnames,
                            extrasaction="ignore")
    writer.writeheader()
    csv_file.flush()

    rows = []
    t_batch_start = time.time()
    for i, student in enumerate(all_students, 1):
        t_student_start = time.time()
        elapsed = t_student_start - t_batch_start
        print(f"\n[{i}/{len(all_students)}] {student}  "
              f"(elapsed {elapsed:.0f}s)")
        row = {"student": student}

        # ── Video analysis ────────────────────────────────────────
        if student in video_files and analyzer:
            print(f"  Video: analyzing {os.path.basename(video_files[student])}...")
            try:
                timeline = analyzer.extract_timeline(video_files[student])
                scores, changes, _, _ = score(timeline)
                for k, v in scores.items():
                    row[f"video_{k}"] = v

                changes_path = video_files[student].replace(
                    os.path.splitext(video_files[student])[1],
                    "_changes.json"
                )
                with open(changes_path, "w") as cf:
                    json.dump(changes, cf, indent=1)

                dt = time.time() - t_student_start
                print(f"  Video: {scores.get('leds_activated', '?')} LEDs, "
                      f"timing={scores.get('timing_1hz', '?')}, "
                      f"inner_cw={scores.get('inner_clockwise_sequence', '?')}  "
                      f"({dt:.1f}s)")
            except Exception as e:
                print(f"  Video: FAILED ({e})")
                row["video_error"] = str(e)
        elif student in video_files:
            print(f"  Video: SKIPPED (no calibration)")
        else:
            print(f"  Video: no video found")

        # ── LLM code review ───────────────────────────────────────
        if student in zip_files:
            print(f"  LLM:   sending to {model}...")
            build_dir = tempfile.mkdtemp(prefix=f"review_{student}_")
            try:
                extract_submission(zip_files[student], build_dir)
                t_llm_start = time.time()
                results = review_submission(
                    build_dir, api_key=api_key, model=model,
                    verbose=verbose,
                )
                dt_llm = time.time() - t_llm_start
                passes = 0
                for item_id in RUBRIC_ITEMS:
                    entry = results.get(item_id, {})
                    if isinstance(entry, dict):
                        row[f"llm_{item_id}_verdict"] = entry.get("verdict", "MISSING")
                        row[f"llm_{item_id}_reason"] = entry.get("reason", "")
                        row[f"llm_{item_id}_evidence"] = entry.get("evidence", "")
                        if entry.get("verdict") == "PASS":
                            passes += 1
                    else:
                        row[f"llm_{item_id}_verdict"] = "UNCLEAR"
                        row[f"llm_{item_id}_reason"] = str(entry)
                        row[f"llm_{item_id}_evidence"] = ""

                print(f"  LLM:   {passes}/{len(RUBRIC_ITEMS)} PASS  ({dt_llm:.1f}s)")
            except Exception as e:
                print(f"  LLM:   FAILED ({e})")
                row["llm_error"] = str(e)
                # Rate-limit backoff: if we hit 429, wait before next request
                if "429" in str(e) or "RESOURCE_EXHAUSTED" in str(e):
                    print(f"  (rate limited, waiting 60s...)")
                    time.sleep(60)
            finally:
                shutil.rmtree(build_dir, ignore_errors=True)
        else:
            print(f"  LLM:   no zip found")

        rows.append(row)
        writer.writerow(row)
        csv_file.flush()

        dt_total = time.time() - t_student_start
        print(f"  Done   ({dt_total:.1f}s, CSV updated)")

    csv_file.close()
    print(f"\nResults written to {results_csv}")

    vid_ok = sum(1 for r in rows if any(
        k.startswith("video_") and k != "video_error" for k in r))
    llm_ok = sum(1 for r in rows if any(
        k.startswith("llm_") and k != "llm_error" for k in r))
    total_time = time.time() - t_batch_start
    print(f"\nSummary: {vid_ok} video + {llm_ok} LLM out of {len(rows)} students  "
          f"({total_time:.0f}s total)")


def grade_all(submissions_dir, ccxml_path, dslite_path, results_csv,
              flash=True, video_dir=None, video_duration=VIDEO_DURATION,
              calibration_path=None, camera_device=0):
    """
    Main grading loop: iterate over zips, compile, optionally flash.
    If calibration_path is provided, runs video analysis after recording.
    """
    analyzer = None
    if calibration_path:
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
            ok, err = ensure_infrastructure(build_dir, LAB_NAME)
            if not ok:
                row["compile_errors"] = err
                print(f"  ERROR: {err}")
                results.append(row)
                continue

            # Compile
            try:
                success, stdout, stderr = compile_submission(build_dir, OUTPUT_NAME)
                row["compile_success"] = success
                if success:
                    print(f"  Compile: PASS")
                else:
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
                            build_dir, dslite_path, ccxml_path, OUTPUT_NAME
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

                            if analyzer is not None:
                                try:
                                    print(f"  Analyzing video...")
                                    timeline = analyzer.extract_timeline(video_path)
                                    scores, changes, _, _ = score(timeline)
                                    for k, v in scores.items():
                                        row[f"video_{k}"] = v
                                    changes_path = os.path.join(
                                        video_dir, f"{student}_changes.json"
                                    )
                                    with open(changes_path, "w") as cf:
                                        json.dump(changes, cf, indent=1)
                                    print(f"  Analysis: {scores.get('leds_activated', '?')} LEDs, "
                                          f"timing={scores.get('timing_1hz', '?')}, "
                                          f"inner_cw={scores.get('inner_clockwise_sequence', '?')}")
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
        if analyzer is not None:
            fieldnames.extend(f"video_{k}" for k in SCORE_FIELDS)
            fieldnames.append("video_analysis_error")
        with open(results_csv, "w", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames,
                                    extrasaction="ignore")
            writer.writeheader()
            writer.writerows(results)
        print(f"\nResults written to {results_csv}")

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

    ok, err = ensure_infrastructure(build_dir, LAB_NAME)
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
        success, stdout, stderr = compile_submission(build_dir, OUTPUT_NAME)
    except subprocess.TimeoutExpired:
        print("  FAIL: Compilation timed out (60 s)")
        result["compile"] = "timeout"
        if not keep_build:
            shutil.rmtree(build_dir, ignore_errors=True)
        return result

    result["compile"] = "ok" if success else "fail"
    if success:
        out_file = os.path.join(build_dir, f"{OUTPUT_NAME}.out")
        size = os.path.getsize(out_file) if os.path.isfile(out_file) else 0
        print(f"  PASS  ({OUTPUT_NAME}.out = {size:,} bytes)")
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

        if video_dir is None:
            video_dir = os.path.dirname(os.path.abspath(zip_path))
        os.makedirs(video_dir, exist_ok=True)

        video_path = os.path.join(video_dir, f"{student}.mp4")
        print(f"  Starting {video_duration}s recording -> {video_path}")
        rec_proc = start_recording(video_path, video_duration, camera_device)

        try:
            f_ok, f_out, f_err = flash_firmware(
                build_dir, dslite_path, ccxml_path, OUTPUT_NAME
            )
        except subprocess.TimeoutExpired:
            f_ok, f_err = False, "Flash timed out"

        result["flash"] = "ok" if f_ok else "fail"
        if f_ok:
            print(f"  PASS")
        else:
            print(f"  FAIL: {f_err.strip().split(chr(10))[0]}")

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

    try:
        analyzer = VideoAnalyzer(calibration_path)
        if threshold_override is not None:
            analyzer.outer_threshold = threshold_override
            analyzer.inner_threshold = threshold_override
        timeline = analyzer.extract_timeline(result["video"], verbose=True)
        scores, changes, _, _ = score(timeline)
        result["scores"] = scores
        result["analysis"] = "ok"

        print(f"  t0 offset:   {scores.get('t0_offset', '?')}")
        print(f"  Activated:   {scores.get('leds_activated', '?')}")
        print(f"  Avg on:      {scores.get('avg_leds_on', '?')}")
        print(f"  Distinct:    {scores.get('distinct_rings', '?')}")
        print(f"  Timing:      {scores.get('timing_1hz', '?')} "
              f"({scores.get('timing_interval', '')})")
        print(f"  Inner CW:    {scores.get('inner_clockwise_sequence', '?')}")
        print(f"  Outer CW:    {scores.get('outer_clockwise_sequence', '?')}")
        print(f"  Inner wrap:  {scores.get('inner_sequence_wrap', '?')}")
        print(f"  Outer wrap:  {scores.get('outer_sequence_wrap', '?')}")
        print(f"  Hr@wrap:     {scores.get('hour_increment_at_wrap', '?')}")
        print(f"  Changes:     {scores.get('total_state_changes', '?')}")

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

    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument(
        "--zip",
        help="Process a single student .zip (debug mode)"
    )
    source.add_argument(
        "--submissions-dir",
        help="Directory containing student submission .zip files (batch mode)"
    )
    source.add_argument(
        "--analyze-dir",
        help="Batch-analyze pre-recorded videos in a directory (no compile/flash)"
    )
    source.add_argument(
        "--grade-batch",
        help="Combined grading: video analysis + LLM code review. "
             "Value is the directory of .zip files. "
             "Use --video-dir for pre-recorded videos."
    )

    parser.add_argument(
        "--ccxml", default=DEFAULT_CCXML,
        help="Path to the .ccxml target configuration file for DSLite"
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
        help="Path to calibration JSON (enables video analysis)"
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
        help="Directory to save recorded videos"
    )
    parser.add_argument(
        "--video",
        help="Path to a pre-recorded video (skip flash+record; --zip mode)"
    )
    parser.add_argument(
        "--threshold", type=int, default=None,
        help="Override brightness threshold from calibration (e.g. 180)"
    )
    parser.add_argument(
        "--api-key",
        help="Gemini API key for LLM code review (default: GEMINI_API_KEY env var)"
    )
    parser.add_argument(
        "--model", default=DEFAULT_MODEL,
        help=f"Gemini model for code review (default: {DEFAULT_MODEL})"
    )
    parser.add_argument(
        "--verbose-llm", action="store_true",
        help="Print LLM prompts and raw responses"
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
        print("\n--- Result summary ---")
        summary = {k: v for k, v in result.items() if k != "scores"}
        print(json.dumps(summary, indent=2))
        if "scores" in result:
            print("\n--- Scores ---")
            print(json.dumps(result["scores"], indent=2))
        sys.exit(0 if result.get("compile") == "ok" else 1)

    # ── Analyze-only batch mode ───────────────────────────────────
    if args.analyze_dir:
        if not os.path.isdir(args.analyze_dir):
            print(f"Error: {args.analyze_dir} is not a directory")
            sys.exit(1)
        if not args.calibration:
            print("Error: --calibration is required for --analyze-dir")
            sys.exit(1)
        if not os.path.isfile(args.calibration):
            print(f"Error: calibration file not found: {args.calibration}")
            sys.exit(1)
        analyze_videos(
            video_dir=args.analyze_dir,
            calibration_path=args.calibration,
            results_csv=args.results_csv,
            threshold_override=args.threshold,
        )
        sys.exit(0)

    # ── Combined batch mode ──────────────────────────────────────
    if args.grade_batch:
        if not os.path.isdir(args.grade_batch):
            print(f"Error: {args.grade_batch} is not a directory")
            sys.exit(1)
        api_key = args.api_key or os.environ.get("GEMINI_API_KEY")
        if not api_key:
            print("Error: set GEMINI_API_KEY or pass --api-key for code review")
            sys.exit(1)
        if args.calibration and not os.path.isfile(args.calibration):
            print(f"Error: calibration file not found: {args.calibration}")
            sys.exit(1)
        grade_batch(
            submissions_dir=args.grade_batch,
            video_dir=args.video_dir,
            calibration_path=args.calibration,
            results_csv=args.results_csv,
            api_key=api_key,
            model=args.model,
            threshold_override=args.threshold,
            verbose=args.verbose_llm,
        )
        sys.exit(0)

    # ── Batch compile/flash mode ──────────────────────────────────
    if not os.path.isdir(args.submissions_dir):
        print(f"Error: {args.submissions_dir} is not a directory")
        sys.exit(1)

    ok, msg = verify_toolchain()
    if not ok:
        print(f"Toolchain error: {msg}")
        sys.exit(1)
    print(f"Toolchain OK: {msg}")

    dslite_path = None
    if not args.compile_only:
        dslite_path = find_dslite()
        if not dslite_path:
            print("Warning: DSLite not found. Set DSLITE_PATH env var.")
            print("Continuing in compile-only mode.\n")
        else:
            print(f"DSLite: {dslite_path}")

        if dslite_path and not os.path.isfile(args.ccxml):
            print(f"Error: ccxml file not found: {args.ccxml}")
            sys.exit(1)

    video_dir = None
    if not args.compile_only and dslite_path:
        if args.video_dir:
            video_dir = args.video_dir
        else:
            results_parent = os.path.dirname(os.path.abspath(args.results_csv))
            video_dir = os.path.join(results_parent, "videos")
        os.makedirs(video_dir, exist_ok=True)
        print(f"Videos: {video_dir} ({args.video_duration}s each)")
        if not shutil.which("ffmpeg"):
            print("Warning: ffmpeg not found in PATH. Video recording will fail.")

    if args.calibration:
        if not os.path.isfile(args.calibration):
            print(f"Error: calibration file not found: {args.calibration}")
            sys.exit(1)

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
