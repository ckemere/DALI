"""
LLM-based code review for Lab 3 submissions.

Sends student C source files and README to Google Gemini and returns
structured PASS/FAIL results plus extracted code snippets showing how
each student represented and implemented their FSM.

Lab 3 adds button-controlled time setting to the LED clock from Labs 1-2.
Students implement a debounced button input, long/short press detection,
and a multi-mode FSM (Normal -> Hour-Set -> Minute-Set -> Normal).

The extracted code snippets feed a second "trend analysis" pass that
compares approaches across the entire class.

Requires:
    pip install google-genai
    Environment variable GEMINI_API_KEY (or pass api_key= explicitly).
"""

import json
import os
import re

_GENAI_AVAILABLE = False
try:
    from google import genai
    from google.genai import types
    _GENAI_AVAILABLE = True
except ImportError:
    pass

from assess.code_review import (
    _CODE_EXTENSIONS,
    _DOC_EXTENSIONS,
    _INFRASTRUCTURE_FILES,
    _parse_response,
    _upload_binary_docs,
    DEFAULT_MODEL,
)

from assess.lab3_score import (
    LLM_RUBRIC_ITEMS,
    LLM_RUBRIC_POINTS,
    LLM_RUBRIC_DESCRIPTIONS,
    LLM_MAX_POINTS,
    EC_LLM_RUBRIC_ITEMS,
    EC_LLM_RUBRIC_POINTS,
    EC_LLM_RUBRIC_DESCRIPTIONS,
    EC_LLM_MAX_POINTS,
    ALL_LLM_ITEMS,
)


# ── Prompt components ─────────────────────────────────────────────

_SYSTEM_PROMPT = """\
You are an expert embedded-systems teaching assistant grading Lab 3
submissions for ELEC 327 at Rice University.

Lab 3 asks students to add button-controlled time setting to their
LED clock firmware (from Labs 1-2).  The target hardware is an
MSPM0G3507 microcontroller driving a custom LED clock board with
24 LEDs (12 outer ring for hours, 12 inner ring for minutes).

The button is on PB8 (active-low with external pull-up).  Students
must implement:

  1. Debouncing — reject presses shorter than ~5 ms.
  2. Long/short press detection — threshold around 1 second.
  3. A finite state machine with modes:
       Normal Clock Mode — clock ticks, short press ignored, long
         press enters Hour-Set.
       Hour-Set Mode — hour LED flashes, short press increments
         hour (12->1 wrap), long press enters Minute-Set.
       Minute-Set Mode — minute LED flashes, short press increments
         minute by 5 (55->0 wrap), long press returns to Normal.
  4. In set modes: clock time is frozen, the relevant LED flashes,
     and the other LED stays steady.
  5. On exit from set mode, the new time becomes active and the
     clock resumes ticking.

Optional extra credit: a Brightness-Set mode after Minute-Set
(long press cycles: Normal -> Hour -> Minute -> Brightness -> Normal).

Students submit C source code and a README explaining their FSM
design and press-detection approach.

IMPORTANT: The defining property of a well-designed state machine is:
  - The CURRENT OUTPUT is fully determined by the current state.
  - The NEXT STATE is fully determined by the current state and the
    current inputs (button, timer).
Look for this property when evaluating fsm_structure.
"""

_RUBRIC_PROMPT = """\
Evaluate the following student submission against EVERY rubric item below.
For EACH item, return a JSON object with these fields:
  "verdict": one of "PASS", "FAIL", or "UNCLEAR"
  "reason":  one-sentence justification
  "evidence": the most relevant quoted line(s) of code or document text
              (empty string if not applicable)

ADDITIONALLY, extract the following code snippets and include them as
top-level keys in your JSON response.  For each, quote the relevant
lines VERBATIM from the student's code (not paraphrased).  If not
found, use an empty string.

  "snippet_state_declaration": The state variable declaration and any
      enum, #define, or constant definitions for FSM states.  Include
      the typedef/enum block if present, or the #defines, or however
      the student names their states.

  "snippet_transition_logic": The core state-transition code — the
      switch/case, if/else chain, or function that decides the next
      state based on current state and button input.  Keep it concise
      (the decision logic, not the full function body).

  "snippet_debounce": The debounce implementation — how the student
      filters out short glitches.  Include the timing comparison or
      counter logic.

  "snippet_press_timing": The long-press vs short-press classification
      code — how the student measures press duration and decides
      which type it is.

  "snippet_flash_mechanism": How the student makes an LED flash in set
      modes — the toggling logic (timer-based, counter-based, etc.).

Return your answer as a JSON object whose keys are the rubric-item IDs
listed below PLUS the five snippet keys above.
Output ONLY valid JSON — no markdown fences, no commentary.

Rubric items:
─────────────
1.  "compiles"
    Does the submission include the expected source files (lab3.c or
    similar main file, plus headers)?  Are there any obvious syntax
    errors that would prevent compilation?  PASS if the code looks
    structurally complete.

2.  "button_gpio_init"
    Is PB8 configured as a GPIO input?  Look for IOMUX configuration
    of the PB8 pin and GPIO input direction setup.  The pin should
    have input enabled (INENA or equivalent).

3.  "fsm_structure"
    Is there an identifiable finite state machine?  Look for a state
    variable (enum, int, #define constants) and transitions driven by
    that variable.  The key property: current outputs should be fully
    determined by the current state, and the next state should be
    fully determined by current state + inputs.  A clean switch/case
    on a state variable is ideal; deeply nested if/else with ad-hoc
    flags is weaker but may still qualify.  PASS if the FSM structure
    is clear and the state variable controls behavior.

4.  "debounce_logic"
    Is button debouncing implemented?  The student should reject
    presses shorter than approximately 5 ms.  Look for a timer-based
    or counter-based filter that requires the button state to be
    stable for a minimum duration before registering as a press.

5.  "long_short_detection"
    Does the code distinguish between long presses (~1 s) and short
    presses?  Look for timing of the press duration (difference
    between press-start and press-end, or a threshold counter).
    PASS if there is a clear threshold comparison around 0.5-2.0 s.

6.  "mode_transitions"
    Are the mode transitions correct?  The sequence should be:
    Normal -> Hour-Set (on long press) -> Minute-Set (on long press)
    -> Normal (on long press).  Short presses should NOT cause mode
    changes in Normal mode.

7.  "hour_increment_logic"
    In Hour-Set mode, does a short press increment the hour?  The
    hour should wrap from 12 (position 11) back to 1 (position 0).

8.  "minute_increment_logic"
    In Minute-Set mode, does a short press increment the minute?
    The minute should advance by 5-minute steps (one LED position),
    wrapping from 55 (position 11) back to 0 (position 0).

9.  "wrap_logic"
    Are both hour wrap (12->1) and minute wrap (55->0) implemented?
    Look for modular arithmetic or explicit boundary checks.

10. "flash_via_fsm"
    Is LED flashing in set modes done through the FSM (e.g., a
    flash counter toggled by the timer interrupt) rather than a
    blocking delay?  FAIL if the code uses delay_cycles() or a
    busy-wait loop to create the flash.

11. "time_freeze_in_set_mode"
    Does the clock stop advancing while in Hour-Set or Minute-Set
    mode?  Look for a check that skips the normal tick logic when
    not in Normal mode.

12. "time_resume_on_exit"
    When exiting set mode back to Normal, does the newly set time
    become the active time?  The clock should resume ticking from
    the adjusted position.

13. "readme_fsm_description"
    Does the README or design document describe the FSM design?
    Look for a state diagram, state table, or textual description
    of the modes and transitions.

14. "readme_press_detection"
    Does the README explain how long vs. short presses are detected?
    Look for discussion of timing, thresholds, and debounce approach.

EC items (evaluate if present, FAIL if not implemented):
────────────────────────────────────────────────────────
15. "brightness_mode_implemented"
    Is a Brightness-Set mode implemented as a fourth mode after
    Minute-Set?  The transition sequence should be:
    Normal -> Hour -> Minute -> Brightness -> Normal.

16. "brightness_levels_ge_15"
    Are at least 15 discrete brightness levels defined?  Look for
    an array of duty-cycle values or a counter with >= 15 steps.

17. "brightness_wrap_to_min"
    Does brightness wrap from maximum back to minimum on a short
    press at the highest level?
"""


# ── Trend analysis prompt ──────────────────────────────────────────

_TREND_SYSTEM_PROMPT = """\
You are an expert embedded-systems instructor analyzing how a class of
students approached a firmware design problem.  You will receive
extracted code snippets from multiple students showing how each one
implemented a finite state machine for an LED clock with button input.

Your job is to identify patterns, clusters of similar approaches, and
interesting variations.  Be specific about what makes approaches
similar or different.
"""

_TREND_PROMPT_HEADER = """\
Below are extracted code snippets from {n_students} students' Lab 3
submissions.  Each student implemented a button-controlled time-setting
FSM for an LED clock on an MSPM0G3507 microcontroller.

For each student you will see (when available):
  - State declaration (enum, #defines, variable)
  - Transition logic (switch/case, if/else)
  - Debounce implementation
  - Press timing (long vs short classification)
  - Flash mechanism (how set-mode LED flashing works)

Analyze these snippets and produce a JSON object with these keys:

  "approach_clusters": A list of clusters, where each cluster is:
    {{"approach": "short description of the approach",
      "students": ["name1", "name2", ...],
      "detail": "what specifically makes these similar"}}

  "state_representation_summary": How students represent FSM states.
    What fraction use enums vs #defines vs raw integers?  Any other
    patterns?

  "debounce_approaches": Summary of debounce strategies seen.
    Timer-based vs counter-based vs other?  What thresholds?

  "press_detection_approaches": How students detect long vs short.
    Measure-on-release vs threshold-counter vs other?

  "flash_approaches": How students implement LED flashing in set
    modes.  Timer toggle vs counter modulo vs other?

  "notable_implementations": List of 2-5 students whose approaches
    are particularly clean, creative, or unusual, with brief
    explanation of why.

  "common_mistakes": Any patterns of mistakes or weak implementations
    seen across multiple students.

Output ONLY valid JSON — no markdown fences, no commentary.
"""


# ── Snippet keys ───────────────────────────────────────────────────

SNIPPET_KEYS = [
    "snippet_state_declaration",
    "snippet_transition_logic",
    "snippet_debounce",
    "snippet_press_timing",
    "snippet_flash_mechanism",
]


# ── Artifact collection ───────────────────────────────────────────

def collect_artifacts(submission_dir):
    """Walk submission_dir and return (code_files, doc_files) dicts."""
    code_files = {}
    doc_files = {}

    for root, _dirs, files in os.walk(submission_dir):
        for fname in files:
            fpath = os.path.join(root, fname)
            rel = os.path.relpath(fpath, submission_dir)
            ext = os.path.splitext(fname)[1].lower()

            if fname.lower() in _INFRASTRUCTURE_FILES:
                continue

            if ext in _CODE_EXTENSIONS:
                try:
                    with open(fpath, "r", errors="replace") as f:
                        code_files[rel] = f.read()
                except OSError:
                    code_files[rel] = "<could not read>"
            elif ext in _DOC_EXTENSIONS:
                if ext in (".pdf", ".doc", ".docx"):
                    doc_files[rel] = fpath
                else:
                    try:
                        with open(fpath, "r", errors="replace") as f:
                            doc_files[rel] = f.read()
                    except OSError:
                        doc_files[rel] = "<could not read>"

    return code_files, doc_files


def _build_user_prompt(code_files, doc_files):
    """Assemble the user-facing prompt with all artifacts inlined."""
    parts = [_RUBRIC_PROMPT, "\n\n"]

    text_docs = {
        k: v for k, v in doc_files.items()
        if not isinstance(v, str) or not v.startswith("/")
    }
    if text_docs:
        parts.append("═══ STUDENT README / DOCUMENTATION ═══\n")
        for name, content in sorted(text_docs.items()):
            parts.append(f"── {name} ──\n{content}\n\n")
    else:
        parts.append("═══ NO README FOUND ═══\n\n")

    parts.append("═══ STUDENT SOURCE CODE ═══\n")
    for name, content in sorted(code_files.items()):
        parts.append(f"── {name} ──\n{content}\n\n")

    return "".join(parts)


# ── Single-student review ─────────────────────────────────────────

def review_submission(submission_dir, *, api_key=None, model=DEFAULT_MODEL,
                      verbose=False):
    """Send a single student submission to Gemini for review.

    Returns dict with rubric item verdicts AND extracted snippets.
    """
    if not _GENAI_AVAILABLE:
        raise RuntimeError("google-genai required: pip install google-genai")

    api_key = api_key or os.environ.get("GEMINI_API_KEY")
    if not api_key:
        raise RuntimeError("Set GEMINI_API_KEY or pass api_key=")

    code_files, doc_files = collect_artifacts(submission_dir)
    if not code_files:
        raise ValueError(f"No .c/.h files found in {submission_dir}")

    binary_docs = {k: v for k, v in doc_files.items()
                   if isinstance(v, str) and os.path.isfile(v)}
    text_docs = {k: v for k, v in doc_files.items()
                 if k not in binary_docs}

    user_prompt = _build_user_prompt(code_files, text_docs)

    if verbose:
        print("─── PROMPT ───")
        print(user_prompt[:2000], "..." if len(user_prompt) > 2000 else "")
        print("─── END PROMPT ───\n")

    client = genai.Client(api_key=api_key)
    uploaded_parts = _upload_binary_docs(client, binary_docs)
    content_parts = list(uploaded_parts) + [user_prompt]

    response = client.models.generate_content(
        model=model,
        contents=content_parts,
        config=types.GenerateContentConfig(
            system_instruction=_SYSTEM_PROMPT,
            temperature=0.1,
            max_output_tokens=65536,
            thinking_config=types.ThinkingConfig(thinking_budget=0),
            response_mime_type="application/json",
        ),
    )

    raw = response.text
    if verbose:
        print("─── RAW RESPONSE ───")
        print(raw[:3000], "..." if len(raw) > 3000 else "")
        print("─── END RESPONSE ───\n")

    try:
        return _parse_response(raw)
    except json.JSONDecodeError as e:
        raise ValueError(
            f"Could not parse Gemini response: {e}\n"
            f"Raw (first 500): {raw[:500]}"
        )


# ── Trend analysis ─────────────────────────────────────────────────

def analyze_trends(student_snippets, *, api_key=None, model=DEFAULT_MODEL,
                   verbose=False):
    """Send aggregated snippets from all students for trend analysis.

    Args:
        student_snippets: dict mapping student name -> dict of snippet
            keys to code strings (as returned by the per-student review).
        api_key: Gemini API key.
        model: Gemini model name.

    Returns:
        Parsed JSON dict with trend analysis results.
    """
    if not _GENAI_AVAILABLE:
        raise RuntimeError("google-genai required: pip install google-genai")

    api_key = api_key or os.environ.get("GEMINI_API_KEY")
    if not api_key:
        raise RuntimeError("Set GEMINI_API_KEY or pass api_key=")

    n_students = len(student_snippets)
    parts = [_TREND_PROMPT_HEADER.format(n_students=n_students), "\n\n"]

    for name in sorted(student_snippets):
        snippets = student_snippets[name]
        parts.append(f"{'═' * 60}\n")
        parts.append(f"STUDENT: {name}\n")
        parts.append(f"{'═' * 60}\n")
        for skey in SNIPPET_KEYS:
            val = snippets.get(skey, "")
            label = skey.replace("snippet_", "").replace("_", " ").title()
            if val:
                parts.append(f"── {label} ──\n{val}\n\n")
            else:
                parts.append(f"── {label} ──\n(not found)\n\n")

    user_prompt = "".join(parts)

    if verbose:
        print(f"─── TREND PROMPT ({n_students} students, "
              f"{len(user_prompt):,} chars) ───")
        print(user_prompt[:3000], "..." if len(user_prompt) > 3000 else "")
        print("─── END PROMPT ───\n")

    client = genai.Client(api_key=api_key)

    response = client.models.generate_content(
        model=model,
        contents=[user_prompt],
        config=types.GenerateContentConfig(
            system_instruction=_TREND_SYSTEM_PROMPT,
            temperature=0.2,
            max_output_tokens=65536,
            thinking_config=types.ThinkingConfig(thinking_budget=0),
            response_mime_type="application/json",
        ),
    )

    raw = response.text
    if verbose:
        print("─── TREND RESPONSE ───")
        print(raw[:5000], "..." if len(raw) > 5000 else "")
        print("─── END RESPONSE ───\n")

    try:
        return _parse_response(raw)
    except json.JSONDecodeError as e:
        raise ValueError(
            f"Could not parse trend response: {e}\n"
            f"Raw (first 500): {raw[:500]}"
        )


# ── Pretty-print ──────────────────────────────────────────────────

def format_results(results, *, use_color=True):
    """Pretty-print rubric results to a string."""
    green = "\033[92m" if use_color else ""
    red = "\033[91m" if use_color else ""
    yellow = "\033[93m" if use_color else ""
    reset = "\033[0m" if use_color else ""

    lines = []
    all_items = LLM_RUBRIC_ITEMS + EC_LLM_RUBRIC_ITEMS
    for item_id in all_items:
        entry = results.get(item_id, {})
        if isinstance(entry, str):
            verdict, reason, evidence = "UNCLEAR", entry, ""
        else:
            verdict = entry.get("verdict", "MISSING")
            reason = entry.get("reason", "")
            evidence = entry.get("evidence", "")

        if verdict == "PASS":
            color = green
        elif verdict == "FAIL":
            color = red
        else:
            color = yellow

        tag = f"{color}{verdict:>7}{reset}"
        lines.append(f"  {tag}  {item_id}")
        if reason:
            lines.append(f"           {reason}")
        if evidence:
            for eline in evidence.split("\n")[:3]:
                lines.append(f"           > {eline}")
        lines.append("")

    # Show snippets if present.
    has_snippets = any(results.get(k) for k in SNIPPET_KEYS)
    if has_snippets:
        lines.append("  ── Extracted Snippets ──")
        for skey in SNIPPET_KEYS:
            val = results.get(skey, "")
            label = skey.replace("snippet_", "")
            if val:
                preview = val[:120].replace("\n", " ↵ ")
                lines.append(f"  {label}: {preview}...")
            else:
                lines.append(f"  {label}: (not found)")
        lines.append("")

    return "\n".join(lines)
