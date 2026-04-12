"""
Upload Lab 2 grades and feedback to Canvas.

Reads the grading CSV produced by ``grading.lab2.score_results``, zips
each student's per-phase videos together with the per-student grade
report into a feedback bundle, and uploads grades + feedback to Canvas
via the REST API.

Unlike Lab 1 (a single video per student), Lab 2 has three per-phase
videos living under ``videos/phase1``, ``videos/phase2``, and
``videos/phase3``.  The feedback zip includes all three phase videos
that exist plus the single shared ``reports/<student>_report.txt``.

Lab 2 also has three Canvas assignments (one per phase).  The typical
workflow is to upload the grand total plus the full bundle to the
Phase 3 assignment (where the writeup lives), since the per-phase
video scores and LLM rubric results are combined in the single report
file.  If you want a different phase, pass its assignment id.

Usage:
    python -m grading.lab2.canvas_upload \\
        --csv grades.csv \\
        --reports-dir reports/ \\
        --video-dir videos/ \\
        --course-id 12345 \\
        --assignment-id 510247

    # Preview without uploading:
    python -m grading.lab2.canvas_upload \\
        --csv grades.csv \\
        --reports-dir reports/ \\
        --video-dir videos/ \\
        --dry-run

Requires:
    Environment variable CANVAS_API_TOKEN (or --token).
    Environment variable CANVAS_BASE_URL (or --url).
    Environment variable COURSE_ID       (or --course-id).
"""

import argparse
import csv
import os
import sys
import tempfile
import zipfile

try:
    import requests
    _REQUESTS_AVAILABLE = True
except ImportError:
    _REQUESTS_AVAILABLE = False

# Re-use shared Canvas helpers.
from grading.canvas import (
    fetch_student_map,
    upload_grade,
    resolve_user_id,
)
from assess.build import student_name_from_zip


_VIDEO_EXTS = {".mp4", ".avi", ".mov", ".mkv"}
_PHASES = ("phase1", "phase2", "phase3")


def _find_phase_video(video_dir, phase, student):
    """Find a student's video for a single phase.

    Looks under ``<video_dir>/<phase>/`` for a video file whose stem
    matches ``student`` — either exactly, or after running the stem
    through :func:`assess.build.student_name_from_zip`.  The latter
    path lets us match Canvas-style long filenames such as
    ``addepallimilan_78839_7441683_Lab_2_-_Phase_1_ma200.mp4`` that
    earlier pipeline stages may have left in place.  Returns the path
    or None.
    """
    if not video_dir:
        return None
    phase_dir = os.path.join(video_dir, phase)
    if not os.path.isdir(phase_dir):
        return None
    for f in os.listdir(phase_dir):
        name, ext = os.path.splitext(f)
        if ext.lower() not in _VIDEO_EXTS:
            continue
        if name == student or student_name_from_zip(name) == student:
            return os.path.join(phase_dir, f)
    return None


def build_feedback_zip(student, report_path, phase_videos):
    """Create a temp zip with the report and any phase videos found.

    Args:
        student:      Canonical student name (used in zip filenames).
        report_path:  Path to ``<student>_report.txt`` or None.
        phase_videos: dict mapping phase name -> video path (values
                      may be None for phases where no video exists).

    Returns:
        Path to the temporary zip file.  The caller is responsible for
        deleting it.
    """
    tmp = tempfile.NamedTemporaryFile(
        prefix=f"{student}_feedback_", suffix=".zip", delete=False)
    tmp.close()
    with zipfile.ZipFile(tmp.name, "w", zipfile.ZIP_DEFLATED) as zf:
        if report_path and os.path.isfile(report_path):
            zf.write(report_path, f"{student}_report.txt")
        for phase, vpath in phase_videos.items():
            if vpath and os.path.isfile(vpath):
                ext = os.path.splitext(vpath)[1]
                zf.write(vpath, f"{student}_{phase}{ext}")
    return tmp.name


def upload_grades(csv_path, reports_dir=None, video_dir=None,
                  course_id=None, assignment_id=None,
                  api_url=None, api_token=None,
                  score_column=None, dry_run=False):
    """
    Upload grades and Lab 2 feedback bundles from a CSV to Canvas.

    Args:
        csv_path:       Path to grades CSV (from score_results).
        reports_dir:    Directory of ``<student>_report.txt`` files.
        video_dir:      Root ``videos/`` directory containing
                        ``phase1/``, ``phase2/``, ``phase3/`` subdirs.
        course_id:      Canvas course ID.
        assignment_id:  Canvas assignment ID (typically the Phase 3
                        assignment, where the writeup lives).
        api_url:        Canvas API base URL.
        api_token:      Canvas API token.
        score_column:   CSV column to use as the grade.  If None,
                        auto-detects a ``grand_total*`` column.
        dry_run:        Print what would be uploaded without acting.
    """
    if not _REQUESTS_AVAILABLE and not dry_run:
        raise RuntimeError("requests package is required: pip install requests")

    # Read CSV.
    with open(csv_path, "r", newline="") as f:
        reader = csv.DictReader(f)
        rows = list(reader)
        fieldnames = reader.fieldnames or []

    # Auto-detect score column.
    if not score_column:
        for fn in fieldnames:
            if fn.startswith("grand_total"):
                score_column = fn
                break
    if not score_column or score_column not in fieldnames:
        print(f"Error: could not find a grand_total column in CSV.\n"
              f"Available columns: {', '.join(fieldnames)}")
        sys.exit(1)

    print(f"CSV: {csv_path} ({len(rows)} students)")
    print(f"Score column: {score_column}")
    if dry_run:
        print("DRY RUN — no changes will be made to Canvas\n")

    # Set up Canvas session.
    session = None
    student_map = None
    if not dry_run:
        session = requests.Session()
        session.headers["Authorization"] = f"Bearer {api_token}"
        print("Fetching Canvas roster...")
        student_map = fetch_student_map(session, api_url, course_id)
        print(f"  Found {len(student_map)} student name mappings\n")

    uploaded = 0
    skipped = 0
    temp_zips = []

    for row in rows:
        student = row.get("student", "")
        score_val = row.get(score_column, "")
        if not student:
            continue

        # Per-student report file.
        report_path = None
        if reports_dir:
            rp = os.path.join(reports_dir, f"{student}_report.txt")
            if os.path.isfile(rp):
                report_path = rp

        # Per-phase video files (any subset may be missing).
        phase_videos = {
            phase: _find_phase_video(video_dir, phase, student)
            for phase in _PHASES
        }
        phases_with_video = [p for p, v in phase_videos.items() if v]

        # Build the feedback zip if there's anything to attach.
        zip_path = None
        if report_path or phases_with_video:
            zip_path = build_feedback_zip(student, report_path, phase_videos)
            temp_zips.append(zip_path)

        if dry_run:
            zip_size = (f"{os.path.getsize(zip_path) / 1024:.0f} KB"
                        if zip_path else "no zip")
            phases_str = (",".join(phases_with_video)
                          if phases_with_video else "none")
            print(f"  {student}: score={score_val}  "
                  f"report={'yes' if report_path else 'no'}  "
                  f"videos={phases_str}  ({zip_size})")
            continue

        # Resolve Canvas user ID.
        user_id = resolve_user_id(student, student_map)
        if not user_id:
            print(f"  {student}: SKIPPED (not found in Canvas roster)")
            skipped += 1
            continue

        try:
            upload_grade(
                session, api_url, course_id, assignment_id,
                user_id, score_val,
                comment_text=f"Lab 2 automated feedback ({score_val} pts)",
                attachment_path=zip_path,
            )
            print(f"  {student}: uploaded (score={score_val})")
            uploaded += 1
        except Exception as e:
            print(f"  {student}: FAILED ({e})")
            skipped += 1

    # Clean up temp zips.
    for zp in temp_zips:
        try:
            os.unlink(zp)
        except OSError:
            pass

    print(f"\nDone: {uploaded} uploaded, {skipped} skipped")


def main():
    parser = argparse.ArgumentParser(
        description="Upload Lab 2 grades and feedback bundles to Canvas")
    parser.add_argument(
        "--csv", required=True,
        help="Path to grades CSV from score_results")
    parser.add_argument(
        "--reports-dir",
        help="Directory containing <student>_report.txt files "
             "(default: reports/)")
    parser.add_argument(
        "--video-dir",
        help="Root videos/ directory (with phase1/, phase2/, phase3/ "
             "subdirectories)")
    parser.add_argument(
        "--course-id", type=int,
        help="Canvas course ID (default: COURSE_ID env var)")
    parser.add_argument(
        "--assignment-id", type=int, required=True,
        help="Canvas assignment ID (typically the Phase 3 assignment)")
    parser.add_argument(
        "--url",
        help="Canvas base URL (default: CANVAS_BASE_URL env var)")
    parser.add_argument(
        "--token",
        help="Canvas API token (default: CANVAS_API_TOKEN env var)")
    parser.add_argument(
        "--score-column",
        help="CSV column to use as the grade "
             "(auto-detected: first 'grand_total*' column)")
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Show what would be uploaded without actually uploading")

    args = parser.parse_args()

    api_url = (args.url
               or os.environ.get("CANVAS_BASE_URL")
               or os.environ.get("CANVAS_API_URL"))
    api_token = args.token or os.environ.get("CANVAS_API_TOKEN")
    course_id = args.course_id or os.environ.get("COURSE_ID")
    if course_id and not isinstance(course_id, int):
        try:
            course_id = int(course_id)
        except (TypeError, ValueError):
            print(f"Error: COURSE_ID '{course_id}' is not an integer")
            sys.exit(1)

    if not args.dry_run:
        if not api_url:
            print("Error: set CANVAS_BASE_URL or pass --url")
            sys.exit(1)
        if not api_token:
            print("Error: set CANVAS_API_TOKEN or pass --token")
            sys.exit(1)
        if not course_id:
            print("Error: set COURSE_ID or pass --course-id")
            sys.exit(1)

    upload_grades(
        csv_path=args.csv,
        reports_dir=args.reports_dir,
        video_dir=args.video_dir,
        course_id=course_id,
        assignment_id=args.assignment_id,
        api_url=api_url,
        api_token=api_token,
        score_column=args.score_column,
        dry_run=args.dry_run,
    )


if __name__ == "__main__":
    main()
