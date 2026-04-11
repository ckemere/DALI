"""
LLM-based code review for Lab 2 submissions (all three phases).

Sends student C source files from all three firmware phases plus the
writeup to Google Gemini and returns structured PASS/FAIL results for
each rubric item.

Lab 2 has three firmware phases:
  Phase 1 — Lab 1 code recapitulation (LED clock with busy-wait delay)
  Phase 2 — Timer interrupt + standby sleep mode
  Phase 3 — PWM-modulated LED brightness via state machine

The writeup (submitted with Phase 3) should document power consumption
estimates and measurements for all three phases, explain the power
reduction from sleep mode, justify the chosen PWM frequency, and
compare power across phases.

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


# ── Rubric items across all three phases ─────────────────────────

RUBRIC_ITEMS = [
    # Phase 1
    "phase1_compiles",
    "phase1_baseline_documented",
    # Phase 2
    "phase2_compiles",
    "phase2_timer_interrupt",
    "phase2_sleep_mode",
    "phase2_sleep_power_documented",
    "phase2_power_reduction_explained",
    # Phase 3
    "phase3_compiles",
    "phase3_state_machine_pwm",
    "phase3_state_machine_documented",
    "phase3_pwm_frequency_justified",
    "phase3_pwm_power_documented",
    "phase3_cross_phase_comparison",
]

RUBRIC_POINTS = {
    "phase1_compiles":                  1,
    "phase1_baseline_documented":       1,
    "phase2_compiles":                  1,
    "phase2_timer_interrupt":           1,
    "phase2_sleep_mode":                1,
    "phase2_sleep_power_documented":    1,
    "phase2_power_reduction_explained": 1,
    "phase3_compiles":                  1,
    "phase3_state_machine_pwm":         1,
    "phase3_state_machine_documented":  1,
    "phase3_pwm_frequency_justified":   1,
    "phase3_pwm_power_documented":      1,
    "phase3_cross_phase_comparison":    1,
}

RUBRIC_DESCRIPTIONS = {
    "phase1_compiles":                  "Phase 1 code compiles",
    "phase1_baseline_documented":       "Baseline power estimated/measured and documented",
    "phase2_compiles":                  "Phase 2 code compiles",
    "phase2_timer_interrupt":           "Uses timer interrupt (not busy-wait delay)",
    "phase2_sleep_mode":                "Enters standby/sleep between ticks",
    "phase2_sleep_power_documented":    "Sleep-mode power estimated/measured",
    "phase2_power_reduction_explained": "Explains why sleep reduces power",
    "phase3_compiles":                  "Phase 3 code compiles",
    "phase3_state_machine_pwm":         "PWM implemented via state machine",
    "phase3_state_machine_documented":  "State machine diagram/explanation in writeup",
    "phase3_pwm_frequency_justified":   "PWM frequency justified (flicker vs. power)",
    "phase3_pwm_power_documented":      "PWM-phase power estimated/measured",
    "phase3_cross_phase_comparison":    "Power compared across all three phases",
}

RUBRIC_MAX_POINTS = sum(RUBRIC_POINTS.get(k, 1) for k in RUBRIC_ITEMS)


_SYSTEM_PROMPT = """\
You are an expert embedded-systems teaching assistant grading Lab 2
submissions for ELEC 327 at Rice University.

Lab 2 asks students to modify their LED clock firmware (originally from
Lab 1) in three phases to progressively reduce power consumption:

  Phase 1: Recapitulation of Lab 1 — LED clock using busy-wait delays.
  Phase 2: Replace busy-wait with timer interrupt (TimerG0) and standby
           sleep mode (WFI / __wfi() / SCB sleep-on-exit).
  Phase 3: Add PWM modulation of LED brightness (25% duty cycle) using
           a state machine to further reduce power.

The target hardware is an MSPM0G3507 microcontroller driving a custom
LED clock board with 24 LEDs (12 outer ring, 12 inner ring).

Students submit code for all three phases and a writeup (PDF or TXT)
documenting power consumption estimates and measurements.  The writeup
is submitted only with Phase 3.

Expected code files per phase (5 files each):
  initialize_leds.c, initialize_leds.h, lab2.c,
  state_machine_logic.h, state_machine_logic.c
"""

_RUBRIC_PROMPT = """\
Evaluate the following student submission against EVERY rubric item below.
For EACH item, return a JSON object with these fields:
  "verdict": one of "PASS", "FAIL", or "UNCLEAR"
  "reason":  one-sentence justification
  "evidence": the most relevant quoted line(s) of code or document text
              (empty string if not applicable)
  "measured_power_uA": number or null — ONLY for the three
              "..._power_documented" / "..._baseline_documented" items
              (phase1_baseline_documented, phase2_sleep_power_documented,
              phase3_pwm_power_documented).  If the writeup reports a
              specific current measurement for that phase, return it as
              a number in microamps (µA).  Convert from mA if needed
              (e.g. "1.2 mA" -> 1200).  Use the EnergyTrace measurement
              if one is given; otherwise use the student's own estimate.
              Return null if no numeric figure was provided, or for any
              rubric item that is not about power documentation.
  "measurement_method": one of "measured", "estimated", or null —
              same three items only.  "measured" if the number came
              from EnergyTrace or a multimeter reading; "estimated" if
              it came from a datasheet-based calculation; null
              otherwise.

Return your answer as a JSON object whose keys are the rubric-item IDs
listed below.  Output ONLY valid JSON — no markdown fences, no commentary.

Rubric items:
─────────────
1.  "phase1_compiles"
    Does the Phase 1 submission include the correct 5 source files
    (initialize_leds.c, initialize_leds.h, lab2.c,
    state_machine_logic.h, state_machine_logic.c)?  Are there any
    obvious syntax errors that would prevent compilation?  PASS if the
    code looks structurally complete and free of syntax errors.

2.  "phase1_baseline_documented"
    Does the writeup document the baseline (Phase 1) power consumption?
    This should include an estimate based on LED datasheets and/or
    MSPM0+ datasheet, and ideally an EnergyTrace measurement.  PASS if
    the writeup provides at least an estimate OR a measurement of
    Phase 1 power.

3.  "phase2_compiles"
    Does the Phase 2 submission include the correct 5 source files?
    Are there any obvious syntax errors that would prevent compilation?

4.  "phase2_timer_interrupt"
    Does the Phase 2 code use a hardware timer interrupt (e.g.,
    TIMG0, TimerG, or similar peripheral) to generate periodic
    interrupts rather than calling a busy-wait delay function?
    Look for timer peripheral initialization (TIMG0->COUNTERREGS.LOAD,
    timer start, NVIC interrupt enable) and an interrupt handler
    (TIMG0_IRQHandler or similar).  FAIL if the code still uses
    delay_cycles or a busy-wait loop as its primary timing mechanism.

5.  "phase2_sleep_mode"
    Does the Phase 2 code put the processor into a sleep or standby
    mode between timer interrupts?  Look for __wfi(), WFI instruction,
    SCB sleep-on-exit configuration, or SYSCTL sleep mode register
    writes.  PASS if the code enters a low-power state each cycle.

6.  "phase2_sleep_power_documented"
    Does the writeup document the power consumption for the
    sleep-based Phase 2 firmware?  Should include an estimate
    and/or an EnergyTrace measurement.

7.  "phase2_power_reduction_explained"
    Does the writeup explain WHY the sleep-based version consumes less
    power than the busy-wait baseline?  Look for discussion of active
    vs. sleep mode current draw, CPU clock gating, etc.

8.  "phase3_compiles"
    Does the Phase 3 submission include the correct 5 source files?
    Are there any obvious syntax errors that would prevent compilation?

9.  "phase3_state_machine_pwm"
    Is the PWM modulation implemented using a state machine approach
    (as opposed to a simple hardware PWM peripheral)?  The state
    machine should have states for "LED on" and "LED off" portions
    of the PWM cycle, driven by the timer interrupt.  Look for
    state variables, state transitions, and LED toggling within the
    interrupt or main loop tick.

10. "phase3_state_machine_documented"
    Does the writeup include a state machine diagram or textual
    explanation of how the PWM is integrated into the clock state
    machine?  PASS if the writeup explains the PWM state machine
    architecture.

11. "phase3_pwm_frequency_justified"
    Does the writeup justify the chosen PWM frequency?  The
    justification should discuss the trade-off between power savings
    (longer sleep time with lower frequency) and perceptual flicker
    (higher frequency needed to avoid visible strobing).  PASS if
    the writeup provides a specific frequency and explains the
    choice.

12. "phase3_pwm_power_documented"
    Does the writeup document the power consumption for the PWM
    Phase 3 firmware?  Should include an estimate and/or an
    EnergyTrace measurement.

13. "phase3_cross_phase_comparison"
    Does the writeup compare power consumption across all three
    phases (baseline, sleep, and PWM)?  PASS if the writeup includes
    at least a qualitative or quantitative comparison of all three.
"""


def collect_artifacts(phase_dirs, writeup_dir=None):
    """
    Collect code from up to three phase directories and a writeup.

    Args:
        phase_dirs: dict mapping phase name (e.g. "phase1", "phase2",
                    "phase3") to the extracted submission directory path.
        writeup_dir: Directory containing the writeup (defaults to the
                     phase3 directory if present).

    Returns:
        (phase_code, doc_files)
        phase_code: dict mapping phase name -> {filename: content}
        doc_files:  {filename: content_or_path}
    """
    phase_code = {}
    doc_files = {}

    for phase_name, sdir in sorted(phase_dirs.items()):
        if not sdir or not os.path.isdir(sdir):
            continue
        code_files = {}
        for root, _dirs, files in os.walk(sdir):
            for fname in files:
                fpath = os.path.join(root, fname)
                rel = os.path.relpath(fpath, sdir)
                ext = os.path.splitext(fname)[1].lower()

                if fname.lower() in _INFRASTRUCTURE_FILES:
                    continue

                if ext in _CODE_EXTENSIONS:
                    try:
                        with open(fpath, "r", errors="replace") as f:
                            code_files[rel] = f.read()
                    except OSError:
                        code_files[rel] = "<could not read>"

        if code_files:
            phase_code[phase_name] = code_files

    # Collect writeup from writeup_dir or phase3 dir.
    search_dir = writeup_dir or phase_dirs.get("phase3")
    if search_dir and os.path.isdir(search_dir):
        for root, _dirs, files in os.walk(search_dir):
            for fname in files:
                fpath = os.path.join(root, fname)
                rel = os.path.relpath(fpath, search_dir)
                ext = os.path.splitext(fname)[1].lower()
                if ext in _DOC_EXTENSIONS:
                    if ext in (".pdf", ".doc", ".docx"):
                        doc_files[rel] = fpath
                    else:
                        try:
                            with open(fpath, "r", errors="replace") as f:
                                doc_files[rel] = f.read()
                        except OSError:
                            doc_files[rel] = "<could not read>"

    return phase_code, doc_files


def _build_user_prompt(phase_code, doc_files):
    """Assemble the user-facing prompt with all artifacts inlined."""
    parts = [_RUBRIC_PROMPT, "\n\n"]

    # Writeup / design documents.
    text_docs = {
        k: v for k, v in doc_files.items()
        if not isinstance(v, str) or not v.startswith("/")
    }
    if text_docs:
        parts.append("═══ STUDENT WRITEUP / DOCUMENTATION ═══\n")
        for name, content in sorted(text_docs.items()):
            parts.append(f"── {name} ──\n{content}\n\n")
    else:
        parts.append("═══ NO WRITEUP FOUND ═══\n\n")

    # Code for each phase.
    for phase_name in sorted(phase_code.keys()):
        code_files = phase_code[phase_name]
        label = phase_name.replace("_", " ").title()
        parts.append(f"═══ {label} SOURCE CODE ═══\n")
        for fname, content in sorted(code_files.items()):
            parts.append(f"── {fname} ──\n{content}\n\n")

    return "".join(parts)


def review_submission(phase_dirs, *, writeup_dir=None, api_key=None,
                      model=DEFAULT_MODEL, verbose=False):
    """
    Send a single student's Lab 2 submission to Gemini for review.

    Args:
        phase_dirs: dict mapping phase name -> extracted dir path.
        writeup_dir: Optional separate writeup directory.
        api_key:     Gemini API key.
        model:       Gemini model name.
        verbose:     Print prompts/responses.

    Returns:
        dict mapping rubric item IDs to
            {"verdict": "PASS"|"FAIL"|"UNCLEAR",
             "reason": str, "evidence": str}
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

    phase_code, doc_files = collect_artifacts(phase_dirs, writeup_dir)
    if not phase_code:
        raise ValueError("No code files found in any phase directory")

    binary_docs = {k: v for k, v in doc_files.items()
                   if isinstance(v, str) and os.path.isfile(v)}
    text_docs = {k: v for k, v in doc_files.items()
                 if k not in binary_docs}

    user_prompt = _build_user_prompt(phase_code, text_docs)

    if verbose:
        print("─── PROMPT ───")
        print(user_prompt[:3000], "..." if len(user_prompt) > 3000 else "")
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
        results = _parse_response(raw)
    except json.JSONDecodeError as e:
        raise ValueError(
            f"Could not parse Gemini response as JSON: {e}\n"
            f"Raw response (first 500 chars): {raw[:500]}"
        )

    return results


def format_results(results, *, use_color=True):
    """Pretty-print rubric results to a string."""
    lines = []
    green = "\033[92m" if use_color else ""
    red = "\033[91m" if use_color else ""
    yellow = "\033[93m" if use_color else ""
    reset = "\033[0m" if use_color else ""

    for item_id in RUBRIC_ITEMS:
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
            for eline in evidence.split("\n"):
                lines.append(f"           > {eline}")
        lines.append("")

    return "\n".join(lines)
