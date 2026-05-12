"""
Upload Lab 3 grades and feedback to Canvas (two-pass workflow).

Pass 1 — Generate HTML grade pages:
    python -m grading.lab3.canvas_upload --generate \\
        --video-results video_results.json \\
        --html-dir html_reports/ \\
        --video-dir ./videos

    Review the HTML pages in html_reports/, then proceed to pass 2.

Pass 2 — Upload to Canvas:
    python -m grading.lab3.canvas_upload --upload \\
        --video-results video_results.json \\
        --html-dir html_reports/ \\
        --video-dir ./videos \\
        --assignment-id 510247

    Each student gets: numeric score (out of 100), plus a submission
    comment with their HTML grade page and video attached as a zip.

Requires:
    Environment variable CANVAS_API_TOKEN (or --token).
    Environment variable CANVAS_BASE_URL (or --url).
    Environment variable COURSE_ID       (or --course-id).
"""

import argparse
import json
import os
import sys
import tempfile
import zipfile

try:
    import requests
    _REQUESTS_AVAILABLE = True
except ImportError:
    _REQUESTS_AVAILABLE = False

from grading.canvas import (
    fetch_student_map,
    upload_grade,
    resolve_user_id,
)
from assess.build import student_name_from_zip
from grading.lab3.score_results import (
    generate_grades,
    write_html_reports,
    compute_canvas_score,
)


_VIDEO_EXTS = {".mp4", ".avi", ".mov", ".mkv"}


def _find_video(video_dir, student):
    """Find a student's video file in the video directory."""
    if not video_dir or not os.path.isdir(video_dir):
        return None
    for f in os.listdir(video_dir):
        name, ext = os.path.splitext(f)
        if ext.lower() not in _VIDEO_EXTS:
            continue
        if name == student or student_name_from_zip(name) == student:
            return os.path.join(video_dir, f)
    return None


def _build_feedback_zip(student, html_path, video_path):
    """Create a temp zip with the HTML report and optional video."""
    tmp = tempfile.NamedTemporaryFile(
        prefix=f"{student}_feedback_", suffix=".zip", delete=False)
    tmp.close()
    with zipfile.ZipFile(tmp.name, "w", zipfile.ZIP_DEFLATED) as zf:
        if html_path and os.path.isfile(html_path):
            zf.write(html_path, f"{student}_grade.html")
        if video_path and os.path.isfile(video_path):
            ext = os.path.splitext(video_path)[1]
            zf.write(video_path, f"{student}{ext}")
    return tmp.name


def generate(video_results_path, html_dir, video_dir=None):
    """Pass 1: generate HTML grade pages from video results."""
    with open(video_results_path) as f:
        video_results = json.load(f)

    grades = generate_grades(video_results, None, include_ec=True)
    if not grades:
        print("No students found in results.")
        return

    paths = write_html_reports(grades, html_dir)

    # Print score summary.
    for student in sorted(grades):
        g = grades[student]
        score = compute_canvas_score(g)
        video_path = _find_video(video_dir, student) if video_dir else None
        vid_tag = " +video" if video_path else ""
        print(f"  {student}: {score}/100{vid_tag}")

    print(f"\nReview HTML pages in {html_dir}/ before uploading.")


def upload(video_results_path, html_dir, video_dir=None,
           course_id=None, assignment_id=None,
           api_url=None, api_token=None, dry_run=False):
    """Pass 2: upload grades + feedback zips to Canvas."""
    if not _REQUESTS_AVAILABLE and not dry_run:
        raise RuntimeError("requests package is required: pip install requests")

    with open(video_results_path) as f:
        video_results = json.load(f)

    grades = generate_grades(video_results, None, include_ec=True)
    if not grades:
        print("No students found in results.")
        return

    print(f"Uploading {len(grades)} students")
    if dry_run:
        print("DRY RUN -- no changes will be made to Canvas\n")

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

    for student in sorted(grades):
        g = grades[student]
        score = compute_canvas_score(g)

        html_path = os.path.join(html_dir, f"{student}.html")
        if not os.path.isfile(html_path):
            html_path = None

        video_path = _find_video(video_dir, student) if video_dir else None

        zip_path = None
        if html_path or video_path:
            zip_path = _build_feedback_zip(student, html_path, video_path)
            temp_zips.append(zip_path)

        if dry_run:
            zip_size = (f"{os.path.getsize(zip_path) / 1024:.0f} KB"
                        if zip_path else "no zip")
            print(f"  {student}: score={score}  "
                  f"html={'yes' if html_path else 'no'}  "
                  f"video={'yes' if video_path else 'no'}  "
                  f"({zip_size})")
            continue

        user_id = resolve_user_id(student, student_map)
        if not user_id:
            print(f"  {student}: SKIPPED (not found in Canvas roster)")
            skipped += 1
            continue

        try:
            upload_grade(
                session, api_url, course_id, assignment_id,
                user_id, score,
                comment_text=f"Lab 3 automated feedback ({score}/100)",
                attachment_path=zip_path,
            )
            print(f"  {student}: uploaded (score={score})")
            uploaded += 1
        except Exception as e:
            print(f"  {student}: FAILED ({e})")
            skipped += 1

    for zp in temp_zips:
        try:
            os.unlink(zp)
        except OSError:
            pass

    print(f"\nDone: {uploaded} uploaded, {skipped} skipped")


def main():
    parser = argparse.ArgumentParser(
        description="Lab 3 Canvas grade upload (two-pass workflow)")

    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument(
        "--generate", action="store_true",
        help="Pass 1: generate HTML grade pages for review")
    mode.add_argument(
        "--upload", action="store_true",
        help="Pass 2: upload grades and feedback to Canvas")

    parser.add_argument(
        "--video-results", required=True, metavar="FILE",
        help="Path to video_results.json")
    parser.add_argument(
        "--html-dir", default="html_reports",
        help="Directory for HTML grade pages (default: html_reports/)")
    parser.add_argument(
        "--video-dir",
        help="Directory containing per-student video files")

    canvas = parser.add_argument_group("canvas (pass 2 only)")
    canvas.add_argument("--course-id", type=int)
    canvas.add_argument("--assignment-id", type=int)
    canvas.add_argument("--url", help="Canvas API base URL")
    canvas.add_argument("--token", help="Canvas API token")
    canvas.add_argument(
        "--dry-run", action="store_true",
        help="Show what would be uploaded without uploading")

    args = parser.parse_args()

    if args.generate:
        generate(
            video_results_path=args.video_results,
            html_dir=args.html_dir,
            video_dir=args.video_dir,
        )
        return

    # --upload
    api_url = (args.url
               or os.environ.get("CANVAS_BASE_URL")
               or os.environ.get("CANVAS_API_URL"))
    api_token = args.token or os.environ.get("CANVAS_API_TOKEN")
    course_id = args.course_id or os.environ.get("COURSE_ID")
    if course_id and not isinstance(course_id, int):
        try:
            course_id = int(course_id)
        except (TypeError, ValueError):
            sys.exit(f"Error: COURSE_ID '{course_id}' is not an integer")

    if not args.dry_run:
        if not api_url:
            sys.exit("Error: set CANVAS_BASE_URL or pass --url")
        if not api_token:
            sys.exit("Error: set CANVAS_API_TOKEN or pass --token")
        if not course_id:
            sys.exit("Error: set COURSE_ID or pass --course-id")
        if not args.assignment_id:
            sys.exit("Error: --assignment-id is required for upload")

    upload(
        video_results_path=args.video_results,
        html_dir=args.html_dir,
        video_dir=args.video_dir,
        course_id=course_id,
        assignment_id=args.assignment_id,
        api_url=api_url,
        api_token=api_token,
        dry_run=args.dry_run,
    )


if __name__ == "__main__":
    main()
