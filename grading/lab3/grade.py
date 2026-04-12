#!/usr/bin/env python3
"""Lab 3 grading orchestrator.

Right now this only implements ``--capture``: compile each student's
submission once, then record a single continuous video per student
while reflashing the board repeatedly between scripted button-press
segments. Analysis, scoring, LLM review, and Canvas upload will be
added later.

Typical invocation::

    python -m grading.lab3.grade --capture \\
        --submissions ./lab3_submissions \\
        --ccxml   $DALI_ROOT/MSPM0G3507.ccxml \\
        --video-dir ./videos \\
        --keep-builds ./builds \\
        --results-csv capture_results.csv

Quick iteration against a single known-good student, limited to a
short segment subset:

    python -m grading.lab3.grade --capture \\
        --submissions ./lab3_submissions \\
        --ccxml   $DALI_ROOT/MSPM0G3507.ccxml \\
        --video-dir ./videos \\
        --only mysubmission.zip \\
        --segments baseline,debounce_reject,enter_hour_set

Each student run produces:

    videos/<student>.mp4          continuous recording, all segments
    videos/<student>.json         host-side ground-truth timing log

The JSON is the capture orchestrator's own record of what happened
when -- flash start/end wall-clock offsets from recording start,
stimulus token list, per-segment status. The analyzer will later
cross-check its video-derived segment boundaries against this log,
so if the two disagree we know which to believe.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import zipfile
from typing import Dict, List, Optional, Sequence

from grading.build_utils import (
    DEFAULT_CCXML,
    compile_submission,
    ensure_infrastructure,
    extract_submission,
    find_dslite,
    flash_firmware,
    start_recording,
    student_name_from_zip,
)

from .helper_client import HelperClient, HelperError
from .segments import (
    SEGMENTS_BY_NAME,
    Segment,
    estimate_total_s,
    run_stimulus,
    select_segments,
)


# Lab name used to locate template infrastructure files.
LAB_NAME = "lab3"
OUTPUT_NAME = "Lab_3"

# Time budgets. These are deliberately generous -- we SIGINT ffmpeg as
# soon as the segment loop finishes, so overestimates just give us
# headroom and cost nothing.
DEFAULT_FRAMERATE = 30
DEFAULT_VIDEO_SIZE = "640x480"
BOOT_AFTER_FLASH_S = 1.0         # after flash completes, before warmup
TRAILING_OBSERVE_S = 2.0          # extra quiet time after final segment
FFMPEG_SETTLE_S = 3.0             # start_recording already waits this long
FFMPEG_HARD_CAP_S = 3600          # passed as -t; we SIGINT well before this

_VIDEO_EXTS = {".mp4", ".avi", ".mov", ".mkv"}


# ---------------------------------------------------------------------------
# ffmpeg lifecycle helpers
# ---------------------------------------------------------------------------


def stop_recording_signal(
    proc: subprocess.Popen,
    timeout_s: float = 15.0,
) -> tuple[bool, str]:
    """Send SIGINT to ffmpeg and wait for it to finalize the output.

    ffmpeg responds to SIGINT by flushing the encoder, finalizing the
    container, and exiting with rc=0 (occasionally 255). We accept both
    as success. Returns ``(ok, error_message)``.
    """
    if proc is None:
        return False, "no ffmpeg process"
    if proc.poll() is not None:
        # Already exited on its own. Collect and report.
        try:
            _, stderr = proc.communicate(timeout=1.0)
        except Exception:
            stderr = b""
        if proc.returncode in (0, 255):
            return True, ""
        tail = stderr.decode(errors="replace").strip().split("\n")[-3:]
        return False, "\n".join(tail)

    try:
        proc.send_signal(signal.SIGINT)
    except ProcessLookupError:
        return True, ""

    try:
        _, stderr = proc.communicate(timeout=timeout_s)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()
        return False, f"ffmpeg did not exit within {timeout_s:.0f}s after SIGINT"

    if proc.returncode in (0, 255):
        return True, ""
    tail = (stderr or b"").decode(errors="replace").strip().split("\n")[-3:]
    return False, "\n".join(tail) or f"ffmpeg rc={proc.returncode}"


# ---------------------------------------------------------------------------
# Student submission discovery
# ---------------------------------------------------------------------------


def _discover_submissions(
    submissions_dir: str,
    only: Optional[Sequence[str]] = None,
) -> Dict[str, str]:
    """Return ``{student_name: zip_path}`` for every .zip in the dir.

    ``only`` may be a list of zip filenames to limit to (matched by
    basename). Useful for bench testing against a specific student.
    """
    out: Dict[str, str] = {}
    if not os.path.isdir(submissions_dir):
        raise FileNotFoundError(submissions_dir)
    only_set = set(only or ())
    for f in sorted(os.listdir(submissions_dir)):
        if not f.endswith(".zip"):
            continue
        if only_set and f not in only_set:
            continue
        name = student_name_from_zip(f)
        out[name] = os.path.join(submissions_dir, f)
    return out


# ---------------------------------------------------------------------------
# Single-student capture
# ---------------------------------------------------------------------------


def capture_student(
    *,
    student: str,
    zip_path: str,
    segments: Sequence[Segment],
    ccxml_path: str,
    dslite_path: str,
    helper: HelperClient,
    video_dir: str,
    keep_builds_root: Optional[str],
    camera_device: int,
    framerate: int,
    video_size: Optional[str],
) -> Dict[str, object]:
    """Run one student's full capture.

    Returns a result dict suitable for writing as a CSV row. Per-segment
    timing detail is written to ``<video_dir>/<student>.json`` rather
    than stuffed into the row.
    """
    os.makedirs(video_dir, exist_ok=True)
    video_file = f"{student}.mp4"
    video_path = os.path.join(video_dir, video_file)
    meta_path = os.path.join(video_dir, f"{student}.json")

    row: Dict[str, object] = {
        "student": student,
        "zip_file": os.path.basename(zip_path),
        "compile_success": False,
        "compile_errors": "",
        "segments_attempted": 0,
        "segments_flashed": 0,
        "video_file": "",
        "metadata_file": "",
        "total_duration_s": 0.0,
        "notes": "",
    }

    # Build directory: kept across all segments for this student (we
    # only compile once and then reflash the same .out N times).
    if keep_builds_root:
        build_dir = os.path.join(keep_builds_root, student)
        os.makedirs(build_dir, exist_ok=True)
        cleanup_build_dir = False
    else:
        build_dir = tempfile.mkdtemp(prefix=f"lab3_{student}_")
        cleanup_build_dir = True
    row["build_dir"] = build_dir

    t_wall_start = time.monotonic()

    try:
        # 1. Extract zip
        try:
            extracted = extract_submission(zip_path, build_dir)
            print(f"  extracted {len(extracted)} files")
        except zipfile.BadZipFile:
            row["compile_errors"] = "Bad zip file"
            print("  ERROR bad zip")
            return row

        # 2. Copy infrastructure files
        ok, err = ensure_infrastructure(build_dir, LAB_NAME)
        if not ok:
            row["compile_errors"] = err
            print(f"  ERROR {err}")
            return row

        # 3. Compile (once)
        try:
            compile_ok, stdout, stderr = compile_submission(
                build_dir, OUTPUT_NAME)
        except subprocess.TimeoutExpired:
            row["compile_errors"] = "Compilation timed out"
            print("  compile TIMEOUT")
            return row

        row["compile_success"] = compile_ok
        if not compile_ok:
            error_lines = (stderr or "").strip().split("\n")
            row["compile_errors"] = "\n".join(error_lines[:5])
            print("  compile FAIL")
            if error_lines:
                print(f"    {error_lines[0]}")
            return row
        print("  compile PASS")

        # 4. Start recording (one continuous video for all segments)
        print(
            f"  recording -> {video_path} "
            f"({framerate} fps, {video_size or 'default'})"
        )
        rec_proc = start_recording(
            video_path,
            duration=FFMPEG_HARD_CAP_S,
            camera_device=camera_device,
            framerate=framerate,
            video_size=video_size or None,
            # start_recording() already sleeps FFMPEG_SETTLE_S internally.
        )
        if rec_proc is None:
            row["notes"] = "ffmpeg failed to start"
            print("  ERROR ffmpeg failed to start")
            return row

        # 5. Segment loop
        metadata: Dict[str, object] = {
            "student": student,
            "zip_file": os.path.basename(zip_path),
            "video_file": video_file,
            "framerate": framerate,
            "video_size": video_size or "",
            "record_epoch_s": time.time(),
            "segments": [],
        }
        seg_results: List[Dict[str, object]] = metadata["segments"]  # type: ignore[assignment]

        # Baseline helper state before any segment: pin Hi-Z, LED off.
        try:
            helper.release()
        except HelperError as e:
            print(f"  WARN helper release failed: {e}")

        rec_t0 = time.monotonic()

        segments_flashed = 0
        try:
            for seg_idx, seg in enumerate(segments, 1):
                row["segments_attempted"] = seg_idx
                print(
                    f"  [{seg_idx}/{len(segments)}] {seg.name}: "
                    f"{seg.description}"
                )

                seg_log: Dict[str, object] = {
                    "index": seg_idx,
                    "name": seg.name,
                    "description": seg.description,
                    "stimulus": list(seg.stimulus),
                    "graded_items": list(seg.graded_items),
                    "warmup_ms": seg.warmup_ms,
                    "observe_ms": seg.observe_ms,
                    "flash_success": False,
                    "flash_errors": "",
                }

                # (a) Flash student board
                seg_log["flash_start_ms"] = int((time.monotonic() - rec_t0) * 1000)
                try:
                    f_ok, f_out, f_err = flash_firmware(
                        build_dir, dslite_path, ccxml_path, OUTPUT_NAME)
                except subprocess.TimeoutExpired:
                    f_ok = False
                    f_err = "flash timed out"
                    f_out = ""
                seg_log["flash_end_ms"] = int((time.monotonic() - rec_t0) * 1000)
                seg_log["flash_success"] = f_ok

                if not f_ok:
                    err_text = ((f_err or f_out) or "").strip()
                    seg_log["flash_errors"] = "\n".join(
                        err_text.split("\n")[:3])
                    print(f"     flash FAIL ({err_text.splitlines()[:1]})")
                    # Skip stimulus + observe for this segment; move on.
                    # The analyzer will see no debug-LED cycle for this
                    # window and score NO_DATA for its rubric items.
                    seg_results.append(seg_log)
                    continue

                segments_flashed += 1
                print(f"     flash PASS")

                # (b) Boot window -- student firmware starting up.
                time.sleep(BOOT_AFTER_FLASH_S)
                seg_log["boot_end_ms"] = int((time.monotonic() - rec_t0) * 1000)

                # (c) Warmup window -- quiet observation of normal mode
                #     before any stimulus.
                time.sleep(seg.warmup_ms / 1000.0)
                seg_log["warmup_end_ms"] = int((time.monotonic() - rec_t0) * 1000)

                # (d) Stimulus. If a protocol error bubbles up, kill
                #     this segment's remaining work but keep the run
                #     going.
                try:
                    run_stimulus(helper, seg.stimulus)
                    stim_error = None
                except HelperError as e:
                    stim_error = str(e)
                    print(f"     WARN stimulus aborted: {e}")
                    # Force release so a stuck mid-press can't persist.
                    try:
                        helper.release()
                    except HelperError:
                        pass
                seg_log["stim_end_ms"] = int((time.monotonic() - rec_t0) * 1000)
                if stim_error:
                    seg_log["stim_error"] = stim_error

                # (e) Observe window -- analyzer reads final state.
                time.sleep(seg.observe_ms / 1000.0)
                seg_log["observe_end_ms"] = int((time.monotonic() - rec_t0) * 1000)

                seg_results.append(seg_log)

            # Small trailing buffer after the last segment so the
            # encoder flushes the final observe window cleanly.
            time.sleep(TRAILING_OBSERVE_S)

        except KeyboardInterrupt:
            row["notes"] = "interrupted"
            print("  INTERRUPTED -- finalizing recording")
            raise
        finally:
            # 6. Always stop recording, even on KeyboardInterrupt.
            v_ok, v_err = stop_recording_signal(rec_proc)
            if v_ok:
                row["video_file"] = video_file
                print(f"  video saved: {video_file}")
            else:
                row["notes"] = (row["notes"] or "") + f" ffmpeg: {v_err}"
                print(f"  video FAILED ({v_err})")

            # Write per-student metadata regardless of video outcome.
            metadata["segments_flashed"] = segments_flashed
            try:
                with open(meta_path, "w") as mf:
                    json.dump(metadata, mf, indent=2)
                row["metadata_file"] = os.path.basename(meta_path)
            except OSError as e:
                print(f"  WARN could not write metadata: {e}")

        row["segments_flashed"] = segments_flashed

    finally:
        row["total_duration_s"] = round(time.monotonic() - t_wall_start, 2)
        if cleanup_build_dir:
            shutil.rmtree(build_dir, ignore_errors=True)

    return row


# ---------------------------------------------------------------------------
# Batch driver
# ---------------------------------------------------------------------------


def capture_batch(
    *,
    submissions_dir: str,
    ccxml_path: str,
    video_dir: str,
    segments: Sequence[Segment],
    only: Optional[Sequence[str]] = None,
    helper_port: Optional[str] = None,
    camera_device: int = 0,
    framerate: int = DEFAULT_FRAMERATE,
    video_size: Optional[str] = DEFAULT_VIDEO_SIZE,
    keep_builds_root: Optional[str] = None,
    results_csv: Optional[str] = None,
) -> List[Dict[str, object]]:
    """Iterate over students and run :func:`capture_student` for each."""

    students = _discover_submissions(submissions_dir, only=only)
    if not students:
        print("No .zip submissions found. Nothing to do.")
        return []

    dslite_path = find_dslite()
    if not dslite_path:
        raise RuntimeError(
            "DSLite not found. Set DSLITE_PATH or add DSLite to PATH."
        )

    est_per = estimate_total_s(segments)
    print(f"Lab 3 capture: {len(students)} student(s), "
          f"{len(segments)} segment(s), "
          f"~{est_per:.0f}s each, "
          f"~{est_per * len(students) / 60:.1f} min total")
    print(f"Segments: {', '.join(s.name for s in segments)}")
    print()

    results: List[Dict[str, object]] = []

    # Helper is opened ONCE per run so the DTR reset-blink only happens
    # before we start recording anyone. The context manager guarantees
    # PB8 is released on exit, even on KeyboardInterrupt.
    try:
        helper = HelperClient.open(port=helper_port)
    except HelperError as e:
        raise RuntimeError(f"could not open helper: {e}") from e

    with helper:
        print(f"helper ready on {helper.port}")
        try:
            helper.ping()
        except HelperError as e:
            raise RuntimeError(f"helper ping failed: {e}") from e

        try:
            for i, (student, zip_path) in enumerate(
                    sorted(students.items()), 1):
                print(f"[{i}/{len(students)}] {student}")
                try:
                    row = capture_student(
                        student=student,
                        zip_path=zip_path,
                        segments=segments,
                        ccxml_path=ccxml_path,
                        dslite_path=dslite_path,
                        helper=helper,
                        video_dir=video_dir,
                        keep_builds_root=keep_builds_root,
                        camera_device=camera_device,
                        framerate=framerate,
                        video_size=video_size,
                    )
                except KeyboardInterrupt:
                    print("interrupted by user")
                    break
                except Exception as e:
                    print(f"  CAPTURE FAILED: {e}")
                    row = {
                        "student": student,
                        "zip_file": os.path.basename(zip_path),
                        "notes": f"exception: {e}",
                    }
                results.append(row)
                print()
        finally:
            # Belt-and-braces release. HelperClient.__exit__ also does
            # this, but being explicit makes the intent obvious.
            try:
                helper.release()
            except HelperError:
                pass

    if results and results_csv:
        _write_csv(results, results_csv)
        print(f"Results written to {results_csv}")

    compiled = sum(1 for r in results if r.get("compile_success"))
    flashed_total = sum(int(r.get("segments_flashed", 0)) for r in results)
    videos = sum(1 for r in results if r.get("video_file"))
    print(
        f"\nSummary: {compiled}/{len(results)} compiled, "
        f"{flashed_total} total segment flashes, "
        f"{videos}/{len(results)} videos recorded"
    )

    return results


def _write_csv(results: Sequence[Dict[str, object]], path: str) -> None:
    fieldnames = [
        "student",
        "zip_file",
        "compile_success",
        "compile_errors",
        "segments_attempted",
        "segments_flashed",
        "video_file",
        "metadata_file",
        "total_duration_s",
        "build_dir",
        "notes",
    ]
    with open(path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(results)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _parse_segment_filter(arg: Optional[str]) -> Optional[List[str]]:
    if not arg:
        return None
    names = [x.strip() for x in arg.split(",") if x.strip()]
    return names or None


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="python -m grading.lab3.grade",
        description="Lab 3 grading orchestrator (capture for now).",
    )

    mode = p.add_argument_group("mode")
    mode.add_argument(
        "--capture", action="store_true",
        help="Compile, flash, record. Required for now (other modes TBD).",
    )

    inp = p.add_argument_group("input")
    inp.add_argument(
        "--submissions", required=False,
        help="Directory of student .zip submissions",
    )
    inp.add_argument(
        "--only", metavar="ZIP", action="append",
        help="Limit to this submission zip filename "
             "(repeatable; matches basename, useful for bench testing)",
    )

    seg = p.add_argument_group("segments")
    seg.add_argument(
        "--segments", metavar="LIST",
        help="Comma-separated segment names. Default: all segments.",
    )
    seg.add_argument(
        "--quick", action="store_true",
        help="Shorthand for --segments baseline,debounce_reject,enter_hour_set",
    )
    seg.add_argument(
        "--list-segments", action="store_true",
        help="Print the segment list and exit",
    )

    hw = p.add_argument_group("hardware")
    hw.add_argument(
        "--ccxml", default=DEFAULT_CCXML,
        help="DSLite CCXML target config "
             f"(default: {DEFAULT_CCXML})",
    )
    hw.add_argument(
        "--helper-port", default=None,
        help="Serial device for the Arduino helper "
             "(default: autodetect by USB VID/PID)",
    )
    hw.add_argument(
        "--camera-device", type=int, default=0,
        help="Camera device index (default 0)",
    )
    hw.add_argument(
        "--framerate", type=int, default=DEFAULT_FRAMERATE,
        help=f"Video frame rate (default {DEFAULT_FRAMERATE})",
    )
    hw.add_argument(
        "--video-size", default=DEFAULT_VIDEO_SIZE,
        help=f"Video capture resolution (default {DEFAULT_VIDEO_SIZE}; "
             "empty string to let ffmpeg pick)",
    )

    out = p.add_argument_group("output")
    out.add_argument(
        "--video-dir", default="./videos",
        help="Where to save <student>.mp4 and <student>.json (default ./videos)",
    )
    out.add_argument(
        "--keep-builds", metavar="DIR",
        help="Preserve build artifacts under DIR/<student>/ for later inspection",
    )
    out.add_argument(
        "--results-csv", metavar="FILE",
        help="Write per-student capture results CSV to FILE",
    )

    return p


def main(argv: Optional[List[str]] = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    if args.list_segments:
        for s in SEGMENTS_BY_NAME.values():
            print(f"  {s.name:<22s}  {s.description}")
            print(f"  {'':<22s}    stimulus={s.stimulus}")
        return 0

    if not args.capture:
        parser.error("no mode selected (use --capture)")

    if not args.submissions:
        parser.error("--submissions is required with --capture")

    names: Optional[List[str]]
    if args.quick:
        names = ["baseline", "debounce_reject", "enter_hour_set"]
    else:
        names = _parse_segment_filter(args.segments)

    try:
        segments = select_segments(names)
    except KeyError as e:
        parser.error(str(e))

    video_size = args.video_size or None

    try:
        capture_batch(
            submissions_dir=args.submissions,
            ccxml_path=args.ccxml,
            video_dir=args.video_dir,
            segments=segments,
            only=args.only,
            helper_port=args.helper_port,
            camera_device=args.camera_device,
            framerate=args.framerate,
            video_size=video_size,
            keep_builds_root=args.keep_builds,
            results_csv=args.results_csv,
        )
    except KeyboardInterrupt:
        print("interrupted")
        return 130
    except Exception as e:
        print(f"error: {e}", file=sys.stderr)
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
