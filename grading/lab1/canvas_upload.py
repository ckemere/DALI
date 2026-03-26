"""
Upload Lab 1 grades and feedback to Canvas.

Reads the grading CSV produced by grade.py, zips each student's video
and grade report into a feedback bundle, and uploads grades + feedback
to Canvas via the REST API.

Usage:
    python -m grading.lab1.canvas_upload \
        --csv results.csv \
        --reports-dir reports/ \
        --video-dir videos/ \
        --course-id 12345 \
        --assignment-id 67890

Requires:
    Environment variable CANVAS_API_TOKEN (or --token).
    Environment variable CANVAS_BASE_URL (or --url, e.g. https://canvas.rice.edu).
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


_VIDEO_EXTS = {".mp4", ".avi", ".mov", ".mkv"}


def _find_video(video_dir, student):
    """Find a video file for a student by name prefix."""
    if not video_dir or not os.path.isdir(video_dir):
        return None
    for f in os.listdir(video_dir):
        name, ext = os.path.splitext(f)
        if ext.lower() in _VIDEO_EXTS and name == student:
            return os.path.join(video_dir, f)
    return None


def build_feedback_zip(student, report_path, video_path):
    """Create a temp zip with the report and optionally the video."""
    tmp = tempfile.NamedTemporaryFile(
        prefix=f"{student}_feedback_", suffix=".zip", delete=False)
    tmp.close()
    with zipfile.ZipFile(tmp.name, "w", zipfile.ZIP_DEFLATED) as zf:
        if report_path and os.path.isfile(report_path):
            zf.write(report_path, f"{student}_report.txt")
        if video_path and os.path.isfile(video_path):
            zf.write(video_path, os.path.basename(video_path))
    return tmp.name


def upload_grades(csv_path, reports_dir=None, video_dir=None,
                  course_id=None, assignment_id=None,
                  api_url=None, api_token=None,
                  score_column=None, dry_run=False):
    """
    Upload grades and feedback from a CSV to Canvas.

    Args:
        csv_path:       Path to results CSV (from grade.py).
        reports_dir:    Directory of *_report.txt files.
        video_dir:      Directory of student videos.
        course_id:      Canvas course ID.
        assignment_id:  Canvas assignment ID.
        api_url:        Canvas API base URL (e.g. https://canvas.rice.edu).
        api_token:      Canvas API token.
        score_column:   CSV column to use as the grade (auto-detected if None).
        dry_run:        If True, print what would be uploaded without doing it.
    """
    if not _REQUESTS_AVAILABLE and not dry_run:
        raise RuntimeError("requests package is required: pip install requests")

    # Read CSV.
    with open(csv_path, "r", newline="") as f:
        reader = csv.DictReader(f)
        rows = list(reader)
        fieldnames = reader.fieldnames

    # Auto-detect score column (prefer grand_total, fall back to llm_total).
    if not score_column:
        for prefix in ("grand_total", "llm_total"):
            for fn in fieldnames:
                if fn.startswith(prefix):
                    score_column = fn
                    break
            if score_column:
                break
    if not score_column:
        print("Error: could not find grand_total or llm_total column in CSV")
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

        # Find report and video files.
        report_path = None
        if reports_dir:
            rp = os.path.join(reports_dir, f"{student}_report.txt")
            if os.path.isfile(rp):
                report_path = rp

        video_path = _find_video(video_dir, student)

        # Build feedback zip.
        zip_path = None
        if report_path or video_path:
            zip_path = build_feedback_zip(student, report_path, video_path)
            temp_zips.append(zip_path)

        if dry_run:
            zip_size = (f"{os.path.getsize(zip_path) / 1024:.0f} KB"
                        if zip_path else "no zip")
            print(f"  {student}: score={score_val}  "
                  f"report={'yes' if report_path else 'no'}  "
                  f"video={'yes' if video_path else 'no'}  "
                  f"({zip_size})")
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
                comment_text=f"Lab 1 automated feedback ({score_val} pts)",
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
        description="Upload Lab 1 grades and feedback to Canvas")
    parser.add_argument(
        "--csv", required=True,
        help="Path to results CSV from grade.py")
    parser.add_argument(
        "--reports-dir",
        help="Directory containing *_report.txt files")
    parser.add_argument(
        "--video-dir",
        help="Directory containing student video files")
    parser.add_argument(
        "--course-id", type=int,
        help="Canvas course ID")
    parser.add_argument(
        "--assignment-id", type=int,
        help="Canvas assignment ID")
    parser.add_argument(
        "--url",
        help="Canvas base URL (default: CANVAS_BASE_URL env var)")
    parser.add_argument(
        "--token",
        help="Canvas API token (default: CANVAS_API_TOKEN env var)")
    parser.add_argument(
        "--score-column",
        help="CSV column to use as the grade (auto-detected if omitted)")
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Show what would be uploaded without actually uploading")

    args = parser.parse_args()

    api_url = args.url or os.environ.get("CANVAS_BASE_URL")
    api_token = args.token or os.environ.get("CANVAS_API_TOKEN")

    if not args.dry_run:
        if not api_url:
            print("Error: set CANVAS_BASE_URL or pass --url")
            sys.exit(1)
        if not api_token:
            print("Error: set CANVAS_API_TOKEN or pass --token")
            sys.exit(1)
        if not args.course_id:
            print("Error: --course-id is required")
            sys.exit(1)
        if not args.assignment_id:
            print("Error: --assignment-id is required")
            sys.exit(1)

    upload_grades(
        csv_path=args.csv,
        reports_dir=args.reports_dir,
        video_dir=args.video_dir,
        course_id=args.course_id,
        assignment_id=args.assignment_id,
        api_url=api_url,
        api_token=api_token,
        score_column=args.score_column,
        dry_run=args.dry_run,
    )


if __name__ == "__main__":
    main()
