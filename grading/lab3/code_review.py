"""
Gemini-based code review for Lab 3 rubric items.

Single-student review primitives live in assess/lab3_code_review.py.
This module adds the bulk review orchestrator (one student at a time)
and re-exports the key symbols for convenience.
"""

from assess.lab3_code_review import (  # noqa: F401
    SNIPPET_KEYS,
    DEFAULT_MODEL,
    collect_artifacts,
    review_submission,
    analyze_trends,
    format_results,
)


def review_bulk(student_dirs, *, api_key=None, model=DEFAULT_MODEL,
                verbose=False):
    """Review multiple students via Gemini, one at a time.

    Args:
        student_dirs: dict mapping student name -> submission directory
            path (extracted zip contents).
        api_key:  Gemini API key (defaults to GEMINI_API_KEY).
        model:    Gemini model name.
        verbose:  Print prompts and raw responses.

    Returns:
        dict mapping student name -> {rubric verdicts + extracted snippets}
    """
    results = {}
    names = sorted(student_dirs)

    for i, name in enumerate(names, 1):
        sub_dir = student_dirs[name]
        print(f"  [{i}/{len(names)}] {name} ...", end=" ", flush=True)
        try:
            r = review_submission(
                sub_dir,
                api_key=api_key,
                model=model,
                verbose=verbose,
            )
            results[name] = r
            n_pass = sum(
                1 for v in r.values()
                if isinstance(v, dict) and v.get("verdict") == "PASS"
            )
            print(f"{n_pass} PASS")
        except Exception as e:
            print(f"ERROR: {e}")
            results[name] = {"_error": str(e)}

    return results
