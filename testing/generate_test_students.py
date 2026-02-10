#!/usr/bin/env python3
"""
Generate fake test students for load testing DALI.

Creates a CSV file with 100 fake students that can be appended to or used
in place of the real student_passwords.csv. All test students have NetIDs
starting with 'test' so they're easy to identify and clean up.

Usage:
    python generate_test_students.py                    # writes test_students.csv
    python generate_test_students.py --count 50         # generate 50 students
    python generate_test_students.py --output roster.csv
"""

import csv
import argparse
import secrets
import string


def generate_password(length=12):
    upper = string.ascii_uppercase
    lower = string.ascii_lowercase
    digits = string.digits
    special = "!@#$%&*-_=+"
    pw = [
        secrets.choice(upper),
        secrets.choice(lower),
        secrets.choice(digits),
        secrets.choice(special),
    ]
    all_chars = upper + lower + digits + special
    pw += [secrets.choice(all_chars) for _ in range(length - 4)]
    secrets.SystemRandom().shuffle(pw)
    return "".join(pw)


def main():
    parser = argparse.ArgumentParser(description="Generate fake test students")
    parser.add_argument("--count", type=int, default=100, help="Number of students (default: 100)")
    parser.add_argument("--output", default="test_students.csv", help="Output CSV path")
    parser.add_argument(
        "--canvas-id-start",
        type=int,
        default=900000,
        help="Starting Canvas ID (default: 900000, well above real IDs)",
    )
    args = parser.parse_args()

    students = []
    for i in range(args.count):
        netid = f"test{i:04d}"
        canvas_id = str(args.canvas_id_start + i)
        name = f"Student, Test{i:04d}"
        password = generate_password()
        students.append({
            "netid": netid,
            "name": name,
            "canvas_id": canvas_id,
            "password": password,
        })

    with open(args.output, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=["netid", "name", "canvas_id", "password"])
        writer.writeheader()
        writer.writerows(students)

    print(f"Generated {args.count} test students → {args.output}")
    print(f"  NetIDs: test0000 .. test{args.count - 1:04d}")
    print(f"  Canvas IDs: {args.canvas_id_start} .. {args.canvas_id_start + args.count - 1}")
    print()
    print("To use with DALI, either:")
    print(f"  1. Set ROSTER_CSV_PATH={args.output}")
    print(f"  2. Append to your real roster: cat {args.output} >> student_passwords.csv")


if __name__ == "__main__":
    main()
