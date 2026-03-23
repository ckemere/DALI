#!/usr/bin/env python3
"""Thin wrapper — see grading/lab1/analyze.py for the actual implementation."""

# Re-export for backward compatibility with existing imports
from grading.video_analyzer import VideoAnalyzer  # noqa: F401
from grading.lab1.score import SCORE_FIELDS  # noqa: F401
from grading.lab1.analyze import main

if __name__ == "__main__":
    main()
