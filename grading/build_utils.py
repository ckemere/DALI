"""
Shared build utilities for grading embedded C labs.

Thin wrapper — canonical implementation is in assess/build.py.
"""

from assess.build import (  # noqa: F401
    DEFAULT_CCXML,
    VIDEO_DURATION,
    INFRASTRUCTURE_FILES,
    find_dslite,
    template_dir_for_lab,
    extract_submission,
    ensure_infrastructure,
    compile_submission,
    flash_firmware,
    start_recording,
    finish_recording,
    student_name_from_zip,
)
