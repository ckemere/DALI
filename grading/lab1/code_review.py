"""
Gemini-based code review for Lab 1 rubric items.

Single-student review primitives live in assess/code_review.py.
This module re-exports them and adds the bulk review orchestrator.
"""

import json
import os

# Re-export all primitives so existing consumers work unchanged.
from assess.code_review import (  # noqa: F401
    RUBRIC_ITEMS,
    RUBRIC_POINTS,
    RUBRIC_DESCRIPTIONS,
    RUBRIC_MAX_POINTS,
    DEFAULT_MODEL,
    collect_artifacts,
    review_submission,
    format_results,
    _parse_response,
    _SYSTEM_PROMPT,
    _RUBRIC_PROMPT,
)

_GENAI_AVAILABLE = False
try:
    from google import genai
    from google.genai import types
    _GENAI_AVAILABLE = True
except ImportError:
    pass


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
    student_sections = {}
    all_binary_parts = []

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

    # ── Build the rubric preamble ──
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
