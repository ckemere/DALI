#!/usr/bin/env python3
"""
fetch_submission_times.py — Fetch submission timestamps from Canvas for an assignment.

Outputs a CSV with student name, Canvas user ID, net ID (login_id), and
submission timestamp.

Usage:
  python fetch_submission_times.py <canvas_assignment_id> [-o output.csv]

Requires environment variables:
  CANVAS_API_TOKEN  — Canvas API bearer token
  COURSE_ID         — Canvas course ID
  CANVAS_BASE_URL   — (optional, default: https://canvas.rice.edu)
"""

import argparse
import csv
import os
import sys

import requests


def require_env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        sys.exit(f"ERROR: Environment variable {name} is not set")
    return value


CANVAS_API_TOKEN = require_env("CANVAS_API_TOKEN")
COURSE_ID = require_env("COURSE_ID")
CANVAS_BASE_URL = os.environ.get("CANVAS_BASE_URL", "https://canvas.rice.edu")


def canvas_get_paginated(endpoint: str) -> list[dict]:
    """GET all pages of a paginated Canvas API endpoint."""
    url = f"{CANVAS_BASE_URL}/api/v1/{endpoint}"
    headers = {"Authorization": f"Bearer {CANVAS_API_TOKEN}"}
    results = []

    while url:
        resp = requests.get(url, headers=headers, params={"per_page": 100}, timeout=30)
        resp.raise_for_status()
        results.extend(resp.json())
        # Canvas pagination: follow the "next" link
        url = None
        if "Link" in resp.headers:
            for part in resp.headers["Link"].split(","):
                if 'rel="next"' in part:
                    url = part.split("<")[1].split(">")[0]
                    break
    return results


def main():
    parser = argparse.ArgumentParser(
        description="Fetch submission timestamps from Canvas for an assignment."
    )
    parser.add_argument(
        "assignment_id",
        help="Canvas assignment ID (e.g. 506143)",
    )
    parser.add_argument(
        "-o", "--output", default=None,
        help="Output CSV path (default: stdout)",
    )
    args = parser.parse_args()

    # Fetch submissions with user info
    print(f"Fetching submissions for assignment {args.assignment_id}...", file=sys.stderr)
    submissions = canvas_get_paginated(
        f"courses/{COURSE_ID}/assignments/{args.assignment_id}"
        f"/submissions?include[]=user"
    )

    # Build rows
    rows = []
    for sub in submissions:
        user = sub.get("user", {})
        submitted_at = sub.get("submitted_at")
        if submitted_at is None:
            continue  # no submission from this student

        rows.append({
            "student_name": user.get("sortable_name", user.get("name", "")),
            "canvas_id": sub.get("user_id", ""),
            "net_id": user.get("login_id", ""),
            "submitted_at": submitted_at,
            "late": sub.get("late", False),
        })

    rows.sort(key=lambda r: r["student_name"])

    # Write CSV
    out = open(args.output, "w", newline="") if args.output else sys.stdout
    writer = csv.DictWriter(out, fieldnames=["student_name", "canvas_id", "net_id", "submitted_at", "late"])
    writer.writeheader()
    writer.writerows(rows)

    if args.output:
        out.close()
        print(f"Wrote {len(rows)} submissions to {args.output}", file=sys.stderr)
    else:
        print(f"\n{len(rows)} submissions with timestamps", file=sys.stderr)


if __name__ == "__main__":
    main()
