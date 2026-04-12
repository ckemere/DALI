"""
Total the points in a rubric YAML file, broken down by category.

Usage:
    python -m grading.lab2.rubric_total rubric.yaml
"""

import sys
import yaml


def main():
    if len(sys.argv) != 2:
        print(f"Usage: python -m grading.lab2.rubric_total <rubric.yaml>",
              file=sys.stderr)
        sys.exit(1)

    with open(sys.argv[1]) as f:
        data = yaml.safe_load(f) or {}

    grand = 0
    print(f"{'category':<16} {'items':>6}  {'points':>7}")
    print("─" * 34)
    for category, entries in data.items():
        if not isinstance(entries, list):
            continue
        subtotal = sum(e.get("points", 0) for e in entries)
        grand += subtotal
        print(f"{category:<16} {len(entries):>6}  {subtotal:>7}")
    print("─" * 34)
    print(f"{'GRAND TOTAL':<16} {'':>6}  {grand:>7}")


if __name__ == "__main__":
    main()
