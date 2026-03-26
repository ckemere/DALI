"""
Shared Canvas LMS API helpers for grade uploading.

Provides:
  - Paginated GET requests
  - Student roster lookup (multiple name formats)
  - Grade + comment/attachment upload
  - A generic CLI upload function that works with any lab's CSV

Requires:
    pip install requests

Environment variables (or pass explicitly):
    CANVAS_API_TOKEN  — Canvas API bearer token
    CANVAS_API_URL    — Canvas base URL (e.g. https://canvas.rice.edu)
    COURSE_ID         — Canvas course ID
"""

import argparse
import csv
import os
import sys

try:
    import requests
    _REQUESTS_AVAILABLE = True
except ImportError:
    _REQUESTS_AVAILABLE = False


# ---------------------------------------------------------------------------
# Low-level API helpers
# ---------------------------------------------------------------------------

def canvas_get(session, base_url, path, params=None):
    """GET with Canvas pagination support."""
    url = f"{base_url}/api/v1{path}"
    results = []
    while url:
        resp = session.get(url, params=params)
        resp.raise_for_status()
        results.extend(resp.json())
        url = None
        link = resp.headers.get("Link", "")
        for part in link.split(","):
            if 'rel="next"' in part:
                url = part.split("<")[1].split(">")[0]
        params = None  # only pass params on first request
    return results


def fetch_student_map(session, base_url, course_id):
    """Return {lookup_key: canvas_user_id} for enrolled students.

    Builds multiple lookup keys per student for flexible matching:
      - sortable_name (lowercase): "last, first"
      - login_id (lowercase): e.g. "abc12"
      - "first_last" style (lowercase, spaces → underscores)
    """
    students = canvas_get(
        session, base_url,
        f"/courses/{course_id}/users",
        params={"enrollment_type[]": "student", "per_page": 200},
    )
    mapping = {}
    for s in students:
        sid = s["id"]
        name = s.get("sortable_name", "").strip().lower()
        login = s.get("login_id", "").strip().lower()
        mapping[name] = sid
        if login:
            mapping[login] = sid
        # Also try "first_last" style.
        parts = name.split(", ", 1)
        if len(parts) == 2:
            mapping[f"{parts[1]}_{parts[0]}".replace(" ", "_")] = sid
    return mapping


def upload_grade(session, base_url, course_id, assignment_id,
                 user_id, score, comment_text=None, attachment_path=None):
    """Submit a grade and optional comment/attachment to Canvas."""
    url = (f"{base_url}/api/v1/courses/{course_id}"
           f"/assignments/{assignment_id}/submissions/{user_id}")

    # Set grade.
    data = {"submission": {"posted_grade": str(score)}}
    resp = session.put(url, json=data)
    resp.raise_for_status()

    # Upload feedback file as a submission comment attachment.
    if attachment_path and os.path.isfile(attachment_path):
        fname = os.path.basename(attachment_path)
        fsize = os.path.getsize(attachment_path)
        upload_url = (f"{base_url}/api/v1/courses/{course_id}"
                      f"/assignments/{assignment_id}"
                      f"/submissions/{user_id}/comments/files")
        resp = session.post(upload_url, json={
            "name": fname,
            "size": fsize,
            "content_type": "application/zip",
        })
        resp.raise_for_status()
        upload_params = resp.json()

        with open(attachment_path, "rb") as f:
            resp2 = requests.post(
                upload_params["upload_url"],
                data=upload_params.get("upload_params", {}),
                files={"file": (fname, f)},
            )
            resp2.raise_for_status()
        file_id = resp2.json()["id"]

        comment_data = {
            "comment": {
                "text_comment": comment_text or "See attached feedback.",
                "file_ids": [file_id],
            }
        }
        resp3 = session.put(url, json=comment_data)
        resp3.raise_for_status()

    elif comment_text:
        comment_data = {
            "comment": {"text_comment": comment_text}
        }
        resp = session.put(url, json=comment_data)
        resp.raise_for_status()


def resolve_user_id(student_key, student_map):
    """Look up a Canvas user ID from a student name or net_id.

    Tries exact match first, then partial matching as a fallback.
    """
    key = student_key.strip().lower()
    user_id = student_map.get(key)
    if not user_id:
        for map_key, uid in student_map.items():
            if key in map_key or map_key in key:
                user_id = uid
                break
    return user_id


# ---------------------------------------------------------------------------
# Generic CSV upload
# ---------------------------------------------------------------------------

def upload_grades_csv(csv_path, course_id, assignment_id,
                      api_url, api_token,
                      student_column="net_id",
                      score_column="score",
                      comment_column=None,
                      dry_run=False):
    """
    Upload grades (and optional comments) from any CSV to Canvas.

    Args:
        csv_path:        Path to grades CSV.
        course_id:       Canvas course ID.
        assignment_id:   Canvas assignment ID.
        api_url:         Canvas API base URL.
        api_token:       Canvas API token.
        student_column:  CSV column identifying the student (matched against
                         Canvas login_id / sortable_name).
        score_column:    CSV column containing the numeric grade.
        comment_column:  CSV column containing text to post as a submission
                         comment (optional).
        dry_run:         If True, print what would be uploaded.
    """
    if not _REQUESTS_AVAILABLE and not dry_run:
        raise RuntimeError("requests package is required: pip install requests")

    # Read CSV.
    with open(csv_path, "r", newline="") as f:
        reader = csv.DictReader(f)
        rows = list(reader)
        fieldnames = reader.fieldnames

    # Validate columns exist.
    if student_column not in fieldnames:
        sys.exit(f"Error: student column '{student_column}' not found in CSV. "
                 f"Available: {', '.join(fieldnames)}")
    if score_column not in fieldnames:
        # Auto-detect: try "score", then "grand_total*", then "total"
        detected = None
        for candidate in ["score", "total"]:
            if candidate in fieldnames:
                detected = candidate
                break
        if not detected:
            for fn in fieldnames:
                if fn.startswith("grand_total"):
                    detected = fn
                    break
        if detected:
            score_column = detected
        else:
            sys.exit(f"Error: score column '{score_column}' not found in CSV. "
                     f"Available: {', '.join(fieldnames)}")

    if comment_column and comment_column not in fieldnames:
        sys.exit(f"Error: comment column '{comment_column}' not found in CSV. "
                 f"Available: {', '.join(fieldnames)}")

    print(f"CSV: {csv_path} ({len(rows)} students)")
    print(f"Student column: {student_column}")
    print(f"Score column: {score_column}")
    if comment_column:
        print(f"Comment column: {comment_column}")
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

    for row in rows:
        student_key = row.get(student_column, "").strip()
        score_val = row.get(score_column, "")
        comment_text = row.get(comment_column, "") if comment_column else None
        if not student_key:
            continue

        if dry_run:
            comment_preview = ""
            if comment_text:
                first_line = comment_text.split("\n")[0]
                comment_preview = f'  comment="{first_line}..."'
            print(f"  {student_key}: score={score_val}{comment_preview}")
            continue

        user_id = resolve_user_id(student_key, student_map)
        if not user_id:
            print(f"  {student_key}: SKIPPED (not found in Canvas roster)")
            skipped += 1
            continue

        try:
            upload_grade(
                session, api_url, course_id, assignment_id,
                user_id, score_val,
                comment_text=comment_text,
            )
            print(f"  {student_key}: uploaded (score={score_val})")
            uploaded += 1
        except Exception as e:
            print(f"  {student_key}: FAILED ({e})")
            skipped += 1

    print(f"\nDone: {uploaded} uploaded, {skipped} skipped")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Upload grades from a CSV to Canvas (generic, works with any lab)")
    parser.add_argument(
        "--csv", required=True,
        help="Path to grades CSV")
    parser.add_argument(
        "--course-id", type=int,
        help="Canvas course ID (default: COURSE_ID env var)")
    parser.add_argument(
        "--assignment-id", type=int, required=True,
        help="Canvas assignment ID")
    parser.add_argument(
        "--url",
        help="Canvas API base URL (default: CANVAS_API_URL env var)")
    parser.add_argument(
        "--token",
        help="Canvas API token (default: CANVAS_API_TOKEN env var)")
    parser.add_argument(
        "--student-column", default="net_id",
        help="CSV column identifying the student (default: net_id)")
    parser.add_argument(
        "--score-column", default="score",
        help="CSV column with the numeric grade (default: score)")
    parser.add_argument(
        "--comment-column", default=None,
        help="CSV column with text to post as a submission comment")
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Show what would be uploaded without actually uploading")

    args = parser.parse_args()

    api_url = args.url or os.environ.get("CANVAS_API_URL") or os.environ.get("CANVAS_BASE_URL")
    api_token = args.token or os.environ.get("CANVAS_API_TOKEN")
    course_id = args.course_id or os.environ.get("COURSE_ID")

    if not args.dry_run:
        if not api_url:
            sys.exit("Error: set CANVAS_API_URL or pass --url")
        if not api_token:
            sys.exit("Error: set CANVAS_API_TOKEN or pass --token")
        if not course_id:
            sys.exit("Error: set COURSE_ID or pass --course-id")

    upload_grades_csv(
        csv_path=args.csv,
        course_id=course_id,
        assignment_id=args.assignment_id,
        api_url=api_url,
        api_token=api_token,
        student_column=args.student_column,
        score_column=args.score_column,
        comment_column=args.comment_column,
        dry_run=args.dry_run,
    )


if __name__ == "__main__":
    main()
