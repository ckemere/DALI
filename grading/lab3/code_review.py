"""
Gemini-based code review for Lab 3 rubric items.

Single-student review primitives live in assess/lab3_code_review.py.
This module adds the bulk review orchestrator (parallel API calls)
and re-exports the key symbols for convenience.
"""

from concurrent.futures import ThreadPoolExecutor, as_completed

from assess.lab3_code_review import (  # noqa: F401
    SNIPPET_KEYS,
    DEFAULT_MODEL,
    collect_artifacts,
    review_submission,
    analyze_trends,
    format_results,
)

MAX_WORKERS = 4


def review_bulk(student_dirs, *, api_key=None, model=DEFAULT_MODEL,
                verbose=False, max_workers=MAX_WORKERS):
    """Review multiple students via Gemini, with parallel API calls.

    Args:
        student_dirs: dict mapping student name -> submission directory
            path (extracted zip contents).
        api_key:  Gemini API key (defaults to GEMINI_API_KEY).
        model:    Gemini model name.
        verbose:  Print prompts and raw responses.
        max_workers: Maximum concurrent Gemini requests.

    Returns:
        dict mapping student name -> {rubric verdicts + extracted snippets}
    """
    results = {}
    names = sorted(student_dirs)
    total = len(names)

    def _review_one(name):
        return name, review_submission(
            student_dirs[name],
            api_key=api_key,
            model=model,
            verbose=verbose,
        )

    print(f"  Reviewing {total} students ({max_workers} parallel)...")

    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = {pool.submit(_review_one, n): n for n in names}
        done = 0
        for future in as_completed(futures):
            name = futures[future]
            done += 1
            try:
                _, r = future.result()
                results[name] = r
                n_pass = sum(
                    1 for v in r.values()
                    if isinstance(v, dict) and v.get("verdict") == "PASS"
                )
                print(f"  [{done}/{total}] {name}: {n_pass} PASS")
            except Exception as e:
                print(f"  [{done}/{total}] {name}: ERROR: {e}")
                results[name] = {"_error": str(e)}

    return results
