"""
Gemini-based code review for Lab 1 rubric items.

Sends student C source files (and optionally a design document) to Google
Gemini and returns structured PASS/FAIL results with quoted evidence for
each rubric item that requires inspecting the source code.

Usage as a library:
    from grading.lab1.code_review import review_submission
    results = review_submission("/path/to/extracted/submission")

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

# File extensions we consider "design document" material.
_DOC_EXTENSIONS = {".txt", ".pdf", ".md", ".doc", ".docx", ".rst"}

# File extensions for C source / headers.
_CODE_EXTENSIONS = {".c", ".h"}

# Infrastructure files that are provided, not student-authored.
_INFRASTRUCTURE_FILES = {
    "startup_mspm0g350x_ticlang.c",
    "mspm0g3507.cmd",
}

# The rubric items that require code / document inspection.
# Each key becomes a field in the output JSON.
RUBRIC_ITEMS = [
    "design_doc_present",
    "diagram_included",
    "state_machine_explanation",
    "writeup_matches_code",
    "code_commentary",
    "power_reset_gpio",
    "iomux_configuration",
    "output_enable_doe",
    "safe_read_modify_write",
    "gpio_state_initialization",
    "init_completeness_24_leds",
    "led_activation_logic",
    "infinite_loop",
    "data_structure_state_machine",
    "timing_delay",
]

# Point value for each rubric item.  Edit weights here.
# Items not listed default to 1 point.
RUBRIC_POINTS = {
    "design_doc_present":        1,
    "diagram_included":          1,
    "state_machine_explanation":  1,
    "writeup_matches_code":      1,
    "code_commentary":           1,
    "power_reset_gpio":          1,
    "iomux_configuration":       1,
    "output_enable_doe":         1,
    "safe_read_modify_write":    1,
    "gpio_state_initialization": 1,
    "init_completeness_24_leds": 1,
    "led_activation_logic":      1,
    "infinite_loop":             1,
    "data_structure_state_machine": 1,
    "timing_delay":              1,
}

# Human-readable descriptions for CSV headers / reports.
RUBRIC_DESCRIPTIONS = {
    "design_doc_present":        "Design document submitted",
    "diagram_included":          "Diagram in design doc",
    "state_machine_explanation":  "State machine explanation",
    "writeup_matches_code":      "Writeup matches code",
    "code_commentary":           "Code comments (why, not just what)",
    "power_reset_gpio":          "GPIO power/reset (GPRCM)",
    "iomux_configuration":       "IOMUX pin configuration",
    "output_enable_doe":         "Output enable (DOE)",
    "safe_read_modify_write":    "Safe read-modify-write",
    "gpio_state_initialization": "GPIO state init before DOE",
    "init_completeness_24_leds": "All 24 LEDs initialized",
    "led_activation_logic":      "Correct LED on/off polarity",
    "infinite_loop":             "Infinite loop structure",
    "data_structure_state_machine": "State machine / data structure",
    "timing_delay":              "Timing delay (~1 Hz)",
}

RUBRIC_MAX_POINTS = sum(RUBRIC_POINTS.get(k, 1) for k in RUBRIC_ITEMS)

# Default Gemini model.  Flash is fast/cheap and sufficient for rubric
# evaluation; students with unlimited academic licenses won't hit quotas.
DEFAULT_MODEL = "gemini-2.5-flash"

_SYSTEM_PROMPT = """\
You are an expert embedded-systems teaching assistant grading Lab 1
submissions for ELEC 327 at Rice University.

Lab 1 asks students to program an MSPM0G3507 microcontroller to drive a
custom LED clock board.  The board has:
  - An OUTER ring of 12 LEDs representing HOURS (on GPIO port A).
  - An INNER ring of 12 LEDs representing SECONDS (on GPIO port A or B).
Two "hands" (one Hour, one Second) sweep around their respective rings at
1 Hz, wrapping from position 12 back to position 1.  At any given moment
exactly 2 LEDs should be on (one per ring).

Students submit:
  1. A design document (text or PDF) with a diagram and state-machine
     explanation.
  2. C source code targeting the TI MSPM0G3507 (registers like GPIOA,
     GPIOB, IOMUX, GPRCM, etc.).
"""

_RUBRIC_PROMPT = """\
Evaluate the following student submission against EVERY rubric item below.
For EACH item, return a JSON object with three fields:
  "verdict": one of "PASS", "FAIL", or "UNCLEAR"
  "reason":  one-sentence justification
  "evidence": the most relevant quoted line(s) of code or document text
              (empty string if not applicable)

Return your answer as a JSON object whose keys are the rubric-item IDs
listed below.  Output ONLY valid JSON — no markdown fences, no commentary.

Rubric items:
─────────────
1.  "design_doc_present"
    Did the student submit a separate text or PDF file explaining their
    design approach?  (Look for any .txt, .pdf, .md, or .docx file that
    is NOT source code.)

2.  "diagram_included"
    Does the design document include or reference a diagram (block diagram,
    state machine, or flow chart)?  For text files, look for ASCII art or
    a mention of an attached figure.  FAIL if no design document exists.

3.  "state_machine_explanation"
    Does the documentation textually explain the states (e.g., "State A
    turns on LED 1…")?  FAIL if no design document exists.

4.  "writeup_matches_code"
    Does the state machine or loop structure described in the design
    document actually match the implementation in the C code?  Compare
    the states, transitions, and overall control flow described in the
    writeup against the actual main loop logic.  PASS if the writeup
    is a reasonable description of what the code does.  FAIL if the
    writeup describes a substantially different approach than what was
    implemented, or if no design document exists.  In your reason,
    briefly note any specific discrepancies you found.

5.  "code_commentary"
    Are comments present in the C code explaining *why* specific hex
    values or registers are used?  (Not just trivial "include header"
    comments.)

6.  "power_reset_gpio"
    Does the code write to GPRCM.RSTCTL *and* GPRCM.PWREN for the
    appropriate GPIO module(s) (GPIOA and/or GPIOB)?

7.  "iomux_configuration"
    Is the IOMUX->SECCFG.PINCM register configured for the specific pins
    used (with the correct function and IOMUX_PINCM_PC_CONNECTED)?

8.  "output_enable_doe"
    Does the code enable the output driver (DOE) for the relevant pins
    using DOESET31_0 or by setting the DOE bits?

9.  "safe_read_modify_write"
    Does the code use safe Read-Modify-Write logic (|= , &=~, or
    DOUTSET/DOUTCLR registers) or other techniques to avoid overwriting
    other potential pins on GPIOA?  Using DOUT31_0 = <value> that
    overwrites the entire port is a FAIL (unless the student explicitly
    accounts for all 32 bits).

10. "gpio_state_initialization"
    Does the initialization set the default output value BEFORE enabling
    the output driver (DOE) so LEDs start OFF?

11. "init_completeness_24_leds"
    Does the initialization code address all 24 LEDs (outer ring + inner
    ring)?  Look for IOMUX configuration of enough pins and DOE for all
    of them.

12. "led_activation_logic"
    To turn an LED ON, does the code use the correct polarity (1 for
    active-high, 0 for active-low, depending on hardware)?  The logic
    should be internally consistent.

13. "infinite_loop"
    Is the main logic wrapped in a while(1), for(;;), or similar infinite
    loop structure?

14. "data_structure_state_machine"
    Does the code use a struct, array, enum, or switch-case to manage
    state rather than just linear if/else chains?

15. "timing_delay"
    Is there a delay mechanism (e.g. delay_cycles, __delay_cycles, or a
    busy-wait loop)?  Compute the expected delay from the code: identify
    the clock frequency (the MSPM0G3507 runs at 32 MHz by default) and
    the cycle count passed to the delay function, then calculate the
    resulting delay in seconds.  Report the computed value in your reason.
    PASS if the computed delay is between 0.5 s and 2.0 s; FAIL
    otherwise (or if no delay mechanism is found).
"""


def collect_artifacts(submission_dir):
    """
    Walk *submission_dir* and return two dicts:
      code_files  : {filename: content_str}   (student-authored .c/.h)
      doc_files   : {filename: content_str}   (design documents)

    Binary files (e.g. .pdf) are noted by name but content is set to
    "<binary file — cannot display inline>".
    """
    code_files = {}
    doc_files = {}

    for root, _dirs, files in os.walk(submission_dir):
        for fname in files:
            fpath = os.path.join(root, fname)
            # Use path relative to submission_dir for display.
            rel = os.path.relpath(fpath, submission_dir)
            ext = os.path.splitext(fname)[1].lower()

            if fname.lower() in _INFRASTRUCTURE_FILES:
                continue  # skip provided files

            if ext in _CODE_EXTENSIONS:
                try:
                    with open(fpath, "r", errors="replace") as f:
                        code_files[rel] = f.read()
                except OSError:
                    code_files[rel] = "<could not read>"
            elif ext in _DOC_EXTENSIONS:
                if ext in (".pdf", ".doc", ".docx"):
                    # We'll upload binary docs as Gemini Part objects later;
                    # store the path so the caller can handle it.
                    doc_files[rel] = fpath  # store path for binary upload
                else:
                    try:
                        with open(fpath, "r", errors="replace") as f:
                            doc_files[rel] = f.read()
                    except OSError:
                        doc_files[rel] = "<could not read>"

    return code_files, doc_files


def _build_user_prompt(code_files, doc_files):
    """
    Assemble the user-facing prompt with all artifacts inlined.

    Binary document files (PDFs) are excluded from the text prompt;
    they should be uploaded as separate parts via the Gemini files API.
    """
    parts = [_RUBRIC_PROMPT, "\n\n"]

    # Design documents (text only — binary handled separately)
    text_docs = {
        k: v for k, v in doc_files.items()
        if not isinstance(v, str) or not v.startswith("/")
    }
    if text_docs:
        parts.append("═══ DESIGN DOCUMENT(S) ═══\n")
        for name, content in sorted(text_docs.items()):
            parts.append(f"── {name} ──\n{content}\n\n")
    else:
        parts.append("═══ NO DESIGN DOCUMENT FOUND ═══\n\n")

    # Source code
    parts.append("═══ STUDENT SOURCE CODE ═══\n")
    for name, content in sorted(code_files.items()):
        parts.append(f"── {name} ──\n{content}\n\n")

    return "".join(parts)


def _parse_response(text):
    """
    Extract the JSON object from Gemini's response.
    Handles optional markdown code fences and common JSON errors.
    """
    # Strip markdown fences if present.
    cleaned = re.sub(r"^```(?:json)?\s*", "", text.strip())
    cleaned = re.sub(r"\s*```$", "", cleaned)

    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        pass

    # Common LLM JSON issues: trailing commas before } or ]
    fixed = re.sub(r",\s*([}\]])", r"\1", cleaned)
    try:
        return json.loads(fixed)
    except json.JSONDecodeError:
        pass

    # Truncated response: try to close open braces/brackets.
    # Count unmatched openers.
    depth_brace = 0
    depth_bracket = 0
    in_string = False
    escape = False
    for ch in fixed:
        if escape:
            escape = False
            continue
        if ch == '\\' and in_string:
            escape = True
            continue
        if ch == '"' and not escape:
            in_string = not in_string
            continue
        if in_string:
            continue
        if ch == '{':
            depth_brace += 1
        elif ch == '}':
            depth_brace -= 1
        elif ch == '[':
            depth_bracket += 1
        elif ch == ']':
            depth_bracket -= 1

    # If we're inside a string (unmatched quote), close it.
    if in_string:
        fixed += '"'
    # Append missing closing tokens.
    fixed += ']' * max(0, depth_bracket)
    fixed += '}' * max(0, depth_brace)
    # Remove trailing commas again after our additions.
    fixed = re.sub(r",\s*([}\]])", r"\1", fixed)

    return json.loads(fixed)


def _upload_binary_docs(client, doc_files):
    """
    Upload binary document files (PDFs, etc.) via the Gemini Files API.
    Returns a list of genai Part references.
    """
    parts = []
    for rel_name, fpath in doc_files.items():
        if isinstance(fpath, str) and os.path.isfile(fpath):
            ext = os.path.splitext(fpath)[1].lower()
            if ext in (".pdf", ".doc", ".docx"):
                try:
                    uploaded = client.files.upload(file=fpath)
                    parts.append(uploaded)
                except Exception as e:
                    print(f"  Warning: could not upload {rel_name}: {e}")
    return parts


def review_submission(submission_dir, *, api_key=None, model=DEFAULT_MODEL,
                      verbose=False):
    """
    Send a student submission to Gemini for code-review grading.

    Args:
        submission_dir: Path to extracted submission files.
        api_key:        Gemini API key (defaults to GEMINI_API_KEY env var).
        model:          Gemini model name.
        verbose:        Print the prompt and raw response.

    Returns:
        dict mapping rubric item IDs to
            {"verdict": "PASS"|"FAIL"|"UNCLEAR",
             "reason": str, "evidence": str}

    Raises:
        RuntimeError if the google-genai package is not installed.
        ValueError if the API response cannot be parsed.
    """
    if not _GENAI_AVAILABLE:
        raise RuntimeError(
            "google-genai package is required.  Install with:\n"
            "  pip install google-genai"
        )

    api_key = api_key or os.environ.get("GEMINI_API_KEY")
    if not api_key:
        raise RuntimeError(
            "Set GEMINI_API_KEY environment variable or pass api_key="
        )

    code_files, doc_files = collect_artifacts(submission_dir)

    if not code_files:
        raise ValueError(f"No .c/.h files found in {submission_dir}")

    # Separate binary docs (paths) from text docs (content strings).
    binary_docs = {k: v for k, v in doc_files.items()
                   if isinstance(v, str) and os.path.isfile(v)}
    text_docs = {k: v for k, v in doc_files.items()
                 if k not in binary_docs}

    user_prompt = _build_user_prompt(code_files, text_docs)

    if verbose:
        print("─── PROMPT ───")
        print(user_prompt[:2000], "..." if len(user_prompt) > 2000 else "")
        print("─── END PROMPT ───\n")

    # Build the Gemini client and request.
    client = genai.Client(api_key=api_key)

    # Upload binary documents (PDFs) if any.
    uploaded_parts = _upload_binary_docs(client, binary_docs)

    # Assemble content parts: uploaded files first, then text prompt.
    content_parts = list(uploaded_parts) + [user_prompt]

    response = client.models.generate_content(
        model=model,
        contents=content_parts,
        config=types.GenerateContentConfig(
            system_instruction=_SYSTEM_PROMPT,
            temperature=0.1,  # low temperature for consistent grading
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
        results = _parse_response(raw)
    except json.JSONDecodeError as e:
        raise ValueError(
            f"Could not parse Gemini response as JSON: {e}\n"
            f"Raw response (first 500 chars): {raw[:500]}"
        )

    return results


def review_bulk(student_dirs, *, api_key=None, model=DEFAULT_MODEL,
                verbose=False):
    """
    Review multiple students via Gemini, auto-chunking to avoid output
    token limits.

    Args:
        student_dirs: dict mapping student name -> extracted submission dir.
        api_key:      Gemini API key (defaults to GEMINI_API_KEY env var).
        model:        Gemini model name.
        verbose:      Print the prompt and raw response.

    Returns:
        dict mapping student name -> {rubric_item_id -> {verdict, reason, evidence}}
    """
    if not _GENAI_AVAILABLE:
        raise RuntimeError(
            "google-genai package is required.  Install with:\n"
            "  pip install google-genai"
        )

    api_key = api_key or os.environ.get("GEMINI_API_KEY")
    if not api_key:
        raise RuntimeError(
            "Set GEMINI_API_KEY environment variable or pass api_key="
        )

    client = genai.Client(api_key=api_key)

    # ── Collect artifacts and build per-student text sections ──
    student_sections = {}   # name -> section text
    all_binary_parts = []   # uploaded PDF parts

    for name, sdir in student_dirs.items():
        code_files, doc_files = collect_artifacts(sdir)
        if not code_files:
            continue

        binary_docs = {k: v for k, v in doc_files.items()
                       if isinstance(v, str) and os.path.isfile(v)}
        text_docs = {k: v for k, v in doc_files.items()
                     if k not in binary_docs}

        for rel_name, fpath in binary_docs.items():
            ext = os.path.splitext(fpath)[1].lower()
            if ext in (".pdf", ".doc", ".docx"):
                try:
                    uploaded = client.files.upload(file=fpath)
                    all_binary_parts.append(uploaded)
                    text_docs[rel_name] = f"<see uploaded file: {rel_name}>"
                except Exception as e:
                    text_docs[rel_name] = f"<upload failed: {e}>"

        section = [f"\n{'═' * 60}\n"]
        section.append(f"STUDENT: {name}\n")
        section.append(f"{'═' * 60}\n")

        if text_docs:
            section.append("── Design Document(s) ──\n")
            for fname, content in sorted(text_docs.items()):
                section.append(f"  [{fname}]\n{content}\n\n")
        else:
            section.append("── No design document found ──\n\n")

        section.append("── Source Code ──\n")
        for fname, content in sorted(code_files.items()):
            section.append(f"  [{fname}]\n{content}\n\n")

        student_sections[name] = "".join(section)

    # ── Build the rubric preamble (shared across chunks) ──
    rubric_items_text = _RUBRIC_PROMPT.split(
        "Rubric items:\n─────────────\n", 1)[-1]

    def _make_bulk_prompt(names):
        header = """\
Evaluate EACH student's submission independently against EVERY rubric item.

For each student and each rubric item, return:
  "verdict": one of "PASS", "FAIL", or "UNCLEAR"
  "reason":  one-sentence justification
  "evidence": the most relevant quoted line(s) from THAT student's code

Return a JSON object whose top-level keys are the student names exactly as
shown (e.g. "alice", "bob"), and each value is an object whose keys are the
rubric-item IDs listed below.

Output ONLY valid JSON — no markdown fences, no commentary.

Rubric items:
─────────────
"""
        parts = [header, rubric_items_text, "\n\n"]
        for n in names:
            parts.append(student_sections[n])
        return "".join(parts)

    # ── Auto-chunk: ~10 students per request ──
    # At ~5K chars output per student, 10 students ≈ 50K chars ≈ 13K tokens,
    # well within the 65K output token limit.
    MAX_STUDENTS_PER_CHUNK = 10
    all_names = list(student_sections.keys())
    chunks = [all_names[i:i + MAX_STUDENTS_PER_CHUNK]
              for i in range(0, len(all_names), MAX_STUDENTS_PER_CHUNK)]

    merged_results = {}

    for chunk_idx, chunk_names in enumerate(chunks, 1):
        user_prompt = _make_bulk_prompt(chunk_names)
        prompt_size = len(user_prompt)

        chunk_label = (f"chunk {chunk_idx}/{len(chunks)}, "
                       f"{len(chunk_names)} students"
                       if len(chunks) > 1 else
                       f"{len(chunk_names)} students")

        if verbose:
            print(f"─── BULK PROMPT ({chunk_label}) ───")
            print(f"Prompt size: {prompt_size:,} chars")
            print(user_prompt[:3000], "..." if len(user_prompt) > 3000 else "")
            print("─── END PROMPT ───\n")
        else:
            print(f"  Prompt: {prompt_size:,} chars, {chunk_label}")

        content_parts = list(all_binary_parts) + [user_prompt]

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
        # Detect output truncation via finish_reason.
        finish_reason = None
        if response.candidates:
            finish_reason = response.candidates[0].finish_reason
        if verbose:
            print("─── RAW RESPONSE ───")
            print(f"  finish_reason: {finish_reason}")
            print(raw[:5000], "..." if len(raw) > 5000 else "")
            print("─── END RESPONSE ───\n")
        if finish_reason and str(finish_reason).upper() in ("MAX_TOKENS", "2"):
            dump_path = f"bulk_response_raw_chunk{chunk_idx}.json"
            with open(dump_path, "w") as df:
                df.write(raw)
            raise ValueError(
                f"Gemini response truncated ({chunk_label}, "
                f"finish_reason={finish_reason}, {len(raw):,} chars).\n"
                f"Raw response saved to {dump_path}"
            )

        try:
            parsed = _parse_response(raw)
        except json.JSONDecodeError as e:
            dump_path = f"bulk_response_raw_chunk{chunk_idx}.json"
            with open(dump_path, "w") as df:
                df.write(raw)
            raise ValueError(
                f"Could not parse Gemini bulk response as JSON ({chunk_label}): {e}\n"
                f"Raw response saved to {dump_path} ({len(raw):,} chars)\n"
                f"First 500 chars: {raw[:500]}"
            )

        merged_results.update(parsed)

    return merged_results


def format_results(results, *, use_color=True):
    """
    Pretty-print rubric results to a string.

    Args:
        results: dict from review_submission().
        use_color: emit ANSI color codes.

    Returns:
        Formatted multi-line string.
    """
    lines = []
    green = "\033[92m" if use_color else ""
    red = "\033[91m" if use_color else ""
    yellow = "\033[93m" if use_color else ""
    reset = "\033[0m" if use_color else ""

    for item_id in RUBRIC_ITEMS:
        entry = results.get(item_id, {})
        if isinstance(entry, str):
            # Gemini sometimes returns a flat string instead of an object.
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
            # Indent quoted evidence.
            for eline in evidence.split("\n"):
                lines.append(f"           > {eline}")
        lines.append("")

    return "\n".join(lines)
