#!/usr/bin/env python3
"""Thin wrapper — see grading/lab1/grade.py for the actual implementation."""

# Re-export for backward compatibility with existing imports
from grading.build_utils import (  # noqa: F401
    extract_submission,
    ensure_infrastructure,
    compile_submission,
    flash_firmware,
)

from grading.lab1.grade import main

if __name__ == "__main__":
    main()
