"""
Per-student assessment primitives for DALI.

This package provides reusable functions for assessing individual student
submissions.  The same primitives are used by:
  - The DALI web app (pre-submission checks, compile queue)
  - The grading bulk-workflow scripts (batch grading)

Submodules:
  assess.build         — Extract, compile, flash embedded C submissions
  assess.pcb           — Parse KiCad PCBs, compute area, run DRC
  assess.video         — LED board video analysis and calibration
  assess.code_review   — LLM-based code review (single student)
  assess.lab1_score    — Lab 1 video timeline scoring
"""
