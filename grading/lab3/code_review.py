"""
Gemini-based code review for Lab 3 rubric items.

Single-student review primitives live in assess/lab3_code_review.py.
This module adds the bulk review orchestrator and trend analysis driver.
"""

import json
import os

from assess.lab3_code_review import (  # noqa: F401
    SNIPPET_KEYS,
    DEFAULT_MODEL,
    collect_artifacts,
    review_submission,
    analyze_trends,
    format_results,
    _parse_response,
    _SYSTEM_PROMPT,
    _RUBRIC_PROMPT,
)

from assess.lab3_score import (
    ALL_LLM_ITEMS,
)

_GENAI_AVAILABLE = False
try:
    from google import genai
    from google.genai import types
    _GENAI_AVAILABLE = True
except ImportError:
    pass

from assess.code_review import _upload_binary_docs


def review_bulk(student_dirs, *, api_key=None, model=DEFAULT_MODEL,
                verbose=False):
    """Review multiple students via Gemini, auto-chunking.

    Args:
        student_dirs: dict mapping student name -> submission directory
            path (extracted zip contents).
        api_key:  Gemini API key (defaults to GEMINI_API_KEY).
        model:    Gemini model name.
        verbose:  Print prompts and raw responses.

    Returns:
        dict mapping student name -> {rubric + snippet results}
    """
    if not _GENAI_AVAILABLE:
        raise RuntimeError("google-genai required: pip install google-genai")

    api_key = api_key or os.environ.get("GEMINI_API_KEY")
    if not api_key:
        raise RuntimeError("Set GEMINI_API_KEY or pass api_key=")

    client = genai.Client(api_key=api_key)

    # Collect per-student text sections.
    student_sections = {}
    all_binary_parts = []

    for name, sub_dir in sorted(student_dirs.items()):
        code_files, doc_files = collect_artifacts(sub_dir)
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
                    text_docs[rel_name] = f"<see uploaded file: {name}/{rel_name}>"
                except Exception as e:
                    text_docs[rel_name] = f"<upload failed: {e}>"

        section = [f"\n{'═' * 60}\n"]
        section.append(f"STUDENT: {name}\n")
        section.append(f"{'═' * 60}\n")

        if text_docs:
            section.append("── README / Documentation ──\n")
            for fname, content in sorted(text_docs.items()):
                section.append(f"  [{fname}]\n{content}\n\n")
        else:
            section.append("── No README found ──\n\n")

        section.append("── Source Code ──\n")
        for fname, content in sorted(code_files.items()):
            section.append(f"  [{fname}]\n{content}\n\n")

        student_sections[name] = "".join(section)

    # Build the rubric text (item descriptions only).
    rubric_items_text = _RUBRIC_PROMPT.split(
        "Rubric items:\n─────────────\n", 1)[-1]

    def _make_bulk_prompt(names):
        header = f"""\
Evaluate EACH student's submission independently against EVERY rubric item.

For each student and each rubric item, return:
  "verdict": one of "PASS", "FAIL", or "UNCLEAR"
  "reason":  one-sentence justification
  "evidence": the most relevant quoted line(s) from THAT student's code

ADDITIONALLY, for each student extract these code snippet keys:
  "snippet_state_declaration", "snippet_transition_logic",
  "snippet_debounce", "snippet_press_timing", "snippet_flash_mechanism"
(verbatim code quotes — empty string if not found)

Return a JSON object whose top-level keys are the student names exactly as
shown, and each value is an object with rubric-item keys AND snippet keys.

Output ONLY valid JSON — no markdown fences, no commentary.

Rubric items:
─────────────
"""
        parts = [header, rubric_items_text, "\n\n"]
        for n in names:
            parts.append(student_sections[n])
        return "".join(parts)

    # Auto-chunk: ~6 students per request.
    MAX_PER_CHUNK = 6
    all_names = list(student_sections.keys())
    chunks = [all_names[i:i + MAX_PER_CHUNK]
              for i in range(0, len(all_names), MAX_PER_CHUNK)]

    merged = {}

    for ci, chunk_names in enumerate(chunks, 1):
        user_prompt = _make_bulk_prompt(chunk_names)
        label = (f"chunk {ci}/{len(chunks)}, {len(chunk_names)} students"
                 if len(chunks) > 1 else f"{len(chunk_names)} students")

        if verbose:
            print(f"─── BULK PROMPT ({label}) ───")
            print(f"Prompt size: {len(user_prompt):,} chars")
            print(user_prompt[:3000], "..." if len(user_prompt) > 3000 else "")
            print("─── END PROMPT ───\n")
        else:
            print(f"  Prompt: {len(user_prompt):,} chars, {label}")

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
        finish = None
        if response.candidates:
            finish = response.candidates[0].finish_reason
        if verbose:
            print(f"─── RAW RESPONSE (finish={finish}) ───")
            print(raw[:5000], "..." if len(raw) > 5000 else "")
            print("─── END RESPONSE ───\n")
        if finish and str(finish).upper() in ("MAX_TOKENS", "2"):
            dump = f"bulk_response_raw_chunk{ci}.json"
            with open(dump, "w") as f:
                f.write(raw)
            raise ValueError(
                f"Gemini response truncated ({label}, "
                f"finish={finish}, {len(raw):,} chars). "
                f"Saved to {dump}")

        try:
            parsed = _parse_response(raw)
        except json.JSONDecodeError as e:
            dump = f"bulk_response_raw_chunk{ci}.json"
            with open(dump, "w") as f:
                f.write(raw)
            raise ValueError(
                f"JSON parse failed ({label}): {e}\n"
                f"Saved to {dump}")

        merged.update(parsed)

    return merged
