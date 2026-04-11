"""
Build, flash, and record primitives for embedded C submissions.

Handles:
  - TI ARM Clang toolchain configuration and Makefile generation
  - Zip extraction and infrastructure file setup
  - Compilation via Make
  - Flashing firmware via DSLite
  - Video recording via ffmpeg

Used by both the DALI web app (compile queue) and grading workflows.
"""

import os
import platform
import shutil
import subprocess
import time
import zipfile


# ---------------------------------------------------------------------------
# TI Toolchain configuration
# ---------------------------------------------------------------------------

TI_COMPILER_ROOT = os.environ.get(
    'TI_COMPILER_ROOT',
    '/opt/ti/ccs/tools/compiler/ti-cgt-armllvm_4.0.4.LTS',
)
TI_SDK_ROOT = os.environ.get(
    'TI_SDK_ROOT',
    '/opt/ti/mspm0_sdk_2_09_00_01',
)

DEVICE_NAME = 'MSPM0G3507'
DEVICE_FAMILY = 'mspm0g1x0x_g3x0x'

CC = f'{TI_COMPILER_ROOT}/bin/tiarmclang'
CFLAGS = [
    '-march=thumbv6m',
    '-mcpu=cortex-m0plus',
    '-mfloat-abi=soft',
    '-mlittle-endian',
    '-mthumb',
    '-Og',
    f'-D__{DEVICE_NAME}__',
    '-g',
    f'-I{TI_SDK_ROOT}/source',
    f'-I{TI_SDK_ROOT}/source/third_party/CMSIS/Core/Include',
    f'-I{TI_SDK_ROOT}/source/ti/devices/msp/m0p/mspm0g350x',
]

LDFLAGS = [
    '-march=thumbv6m',
    '-mcpu=cortex-m0plus',
    '-mfloat-abi=soft',
    '-mlittle-endian',
    '-mthumb',
    '-Wl,--reread_libs',
    '-Wl,--diag_wrap=off',
    '-Wl,--display_error_number',
    '-Wl,--warn_sections',
    '-Wl,--rom_model',
    f'-Wl,-i{TI_COMPILER_ROOT}/lib',
]

LIBRARIES = [
    f'{TI_SDK_ROOT}/source/ti/driverlib/lib/ticlang/m0p/{DEVICE_FAMILY}/driverlib.a'
]

# Paths derived from the repo root (one level up from this file's directory).
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_CCXML = os.path.join(_REPO_ROOT, "MSPM0G3507.ccxml")
VIDEO_DURATION = 150  # seconds to record after flashing

# Infrastructure files that students don't modify.
INFRASTRUCTURE_FILES = [
    "startup_mspm0g350x_ticlang.c",
    f"{DEVICE_NAME.lower()}.cmd",
]


# ---------------------------------------------------------------------------
# Toolchain verification
# ---------------------------------------------------------------------------

def verify_toolchain():
    """
    Verify that TI toolchain is installed and accessible.

    Returns:
        tuple: (success: bool, message: str)
    """
    if not os.path.exists(CC):
        return False, f"Compiler not found at {CC}. Set TI_COMPILER_ROOT environment variable."
    if not os.path.exists(TI_SDK_ROOT):
        return False, f"SDK not found at {TI_SDK_ROOT}. Set TI_SDK_ROOT environment variable."
    driverlib_path = LIBRARIES[0]
    if not os.path.exists(driverlib_path):
        return False, f"Driver library not found at {driverlib_path}. Check SDK installation."
    return True, "Toolchain verified successfully"


# ---------------------------------------------------------------------------
# Makefile generation
# ---------------------------------------------------------------------------

def create_makefile_for_lab(build_dir, source_files, output_name='firmware'):
    """
    Create a Makefile that matches CCS build settings.

    Args:
        build_dir: Directory where Makefile will be created.
        source_files: List of .c files to compile.
        output_name: Name of output file (default: firmware).
    """
    c_files = [f for f in source_files if f.endswith('.c')]
    obj_files = [f.replace('.c', '.o') for f in c_files]
    cmd_file = f'{DEVICE_NAME.lower()}.cmd'

    makefile_content = f"""# DALI - Auto-generated Makefile for {DEVICE_NAME}
# Based on CCS build settings

# Toolchain
CC = {CC}

# Compiler flags
CFLAGS = {' '.join(CFLAGS)}

# Linker flags
LDFLAGS = {' '.join(LDFLAGS)}

# Libraries
LIBS = {' '.join(LIBRARIES)}

# Source files
SRCS = {' '.join(c_files)}

# Object files
OBJS = {' '.join(obj_files)}

# Linker command file
CMD_FILE = {cmd_file}

# Output
TARGET = {output_name}.out

# Default target
all: $(TARGET)

# Link
$(TARGET): $(OBJS) $(CMD_FILE)
\t@echo "Linking $@..."
\t$(CC) $(LDFLAGS) -Wl,-m"{output_name}.map" -o $@ $(OBJS) $(CMD_FILE) $(LIBS)
\t@echo "Build complete: $@"

# Compile .c to .o
%.o: %.c
\t@echo "Compiling $<..."
\t$(CC) $(CFLAGS) -c $< -o $@

# Clean
clean:
\t@echo "Cleaning..."
\trm -f $(OBJS) $(TARGET) {output_name}.map *.d
\t@echo "Clean complete"

# Show configuration (for debugging)
config:
\t@echo "Compiler: $(CC)"
\t@echo "Device: {DEVICE_NAME}"
\t@echo "SDK: {TI_SDK_ROOT}"
\t@echo "Sources: $(SRCS)"
\t@echo "Objects: $(OBJS)"

.PHONY: all clean config
"""

    makefile_path = os.path.join(build_dir, 'Makefile')
    with open(makefile_path, 'w') as f:
        f.write(makefile_content)
    return makefile_path


def ensure_linker_script(build_dir, template_dir):
    """
    Ensure the linker command file (.cmd) is present.
    Copy from template if not present in build directory.
    """
    cmd_filename = f'{DEVICE_NAME.lower()}.cmd'
    build_cmd = os.path.join(build_dir, cmd_filename)

    if not os.path.exists(build_cmd):
        template_cmd = os.path.join(template_dir, cmd_filename)
        if os.path.exists(template_cmd):
            shutil.copy(template_cmd, build_cmd)
            return True
        else:
            raise FileNotFoundError(
                f"Linker script {cmd_filename} not found in build directory or templates. "
                f"This file is required for linking. Please add it to template_files/{template_dir}/"
            )
    return True


def get_compilation_command(build_dir, verbose=False):
    """Get the actual compilation command that will be run."""
    return f"make -C {build_dir} {'VERBOSE=1' if verbose else ''}"


# ---------------------------------------------------------------------------
# Submission handling
# ---------------------------------------------------------------------------

def template_dir_for_lab(lab_name):
    """Return the template directory for a given lab (e.g. 'lab1')."""
    return os.path.join(_REPO_ROOT, "template_files", lab_name)


def extract_submission(zip_path, build_dir):
    """
    Extract a submission zip into build_dir.
    Returns list of extracted filenames.
    """
    with zipfile.ZipFile(zip_path, "r") as zf:
        zf.extractall(build_dir)
        return zf.namelist()


def ensure_infrastructure(build_dir, lab_name):
    """
    Copy infrastructure files (startup, linker script) from template
    if they are not present in the student submission.

    Returns (success: bool, error_message: str).
    """
    tpl_dir = template_dir_for_lab(lab_name)
    for fname in INFRASTRUCTURE_FILES:
        dest = os.path.join(build_dir, fname)
        if not os.path.isfile(dest):
            src = os.path.join(tpl_dir, fname)
            if os.path.isfile(src):
                shutil.copy2(src, dest)
            else:
                return False, f"Missing infrastructure file: {fname}"
    return True, ""


def compile_submission(build_dir, output_name):
    """
    Generate a Makefile and compile.

    Args:
        build_dir:    Directory with source files.
        output_name:  Base name for the output binary (e.g. "Lab_1").

    Returns (success, stdout, stderr).
    """
    source_files = [f for f in os.listdir(build_dir) if f.endswith(".c")]
    if not source_files:
        return False, "", "No .c files found in submission"

    create_makefile_for_lab(build_dir, source_files, output_name)

    proc = subprocess.run(
        ["make", "-C", build_dir, "all"],
        capture_output=True,
        text=True,
        timeout=60,
        env={**os.environ, "PATH": os.environ.get("PATH", "/usr/bin")},
    )
    return proc.returncode == 0, proc.stdout, proc.stderr


# ---------------------------------------------------------------------------
# Flash firmware
# ---------------------------------------------------------------------------

def find_dslite():
    """Locate the DSLite binary from env var or PATH."""
    path = os.environ.get("DSLITE_PATH")
    if path and os.path.isfile(path):
        return path
    return shutil.which("DSLite")


def flash_firmware(build_dir, dslite_path, ccxml_path, output_name):
    """
    Flash the compiled .out file onto the board using DSLite.

    Returns (success, stdout, stderr).
    """
    out_file = os.path.join(build_dir, f"{output_name}.out")
    if not os.path.isfile(out_file):
        return False, "", f"{output_name}.out not found after compilation"

    ccxml_abs = os.path.abspath(ccxml_path)

    proc = subprocess.run(
        [dslite_path, "flash", "--config", ccxml_abs, "-f", out_file],
        capture_output=True,
        text=True,
        timeout=30,
    )
    return proc.returncode == 0, proc.stdout, proc.stderr


# ---------------------------------------------------------------------------
# Video recording
# ---------------------------------------------------------------------------

def start_recording(output_path, duration=VIDEO_DURATION, camera_device=0,
                    settle_time=3, framerate=30,
                    input_format=None, video_size=None):
    """
    Start recording video in the background using ffmpeg.
    Auto-detects platform (Linux v4l2, macOS avfoundation).

    Waits *settle_time* seconds after launching ffmpeg so the camera
    is actually capturing before the caller proceeds.

    Args:
        output_path: Path for the output video file.
        duration:    Recording duration in seconds.
        camera_device: Camera device index.
        settle_time: Seconds to wait after launching ffmpeg.
        framerate:   Capture frame rate (default 30; use higher for
                     PWM flicker detection, e.g. 120 or 240).
        input_format: v4l2/avfoundation pixel format hint (e.g.
                     ``mjpeg``, ``yuyv422``).  Defaults to ``mjpeg`` on
                     Linux because most USB webcams cannot sustain
                     >5 fps in raw YUYV at HD resolution.  Override via
                     the ``DALI_FFMPEG_INPUT_FORMAT`` environment
                     variable; pass an empty string to disable.
        video_size:  Capture resolution (e.g. ``1280x720``,
                     ``640x480``).  Defaults to whatever the
                     ``DALI_FFMPEG_VIDEO_SIZE`` environment variable
                     is set to, otherwise lets ffmpeg pick.  Lower
                     resolutions are usually required to hit very high
                     frame rates (120+ fps for PWM detection).

    Returns a Popen process, or None on error.
    """
    system = platform.system()
    fr = str(framerate)

    # Resolve input format / video size from env-var fallbacks.  An
    # explicit empty string disables the option entirely.
    if input_format is None:
        input_format = os.environ.get("DALI_FFMPEG_INPUT_FORMAT", "mjpeg")
    if video_size is None:
        video_size = os.environ.get("DALI_FFMPEG_VIDEO_SIZE", "")

    if system == "Linux":
        input_args = ["-f", "v4l2"]
        if input_format:
            input_args += ["-input_format", input_format]
        if video_size:
            input_args += ["-video_size", video_size]
        input_args += ["-framerate", fr,
                       "-i", f"/dev/video{camera_device}"]
    elif system == "Darwin":
        input_args = ["-f", "avfoundation"]
        if input_format:
            input_args += ["-pixel_format", input_format]
        if video_size:
            input_args += ["-video_size", video_size]
        input_args += ["-framerate", fr,
                       "-i", str(camera_device)]
    else:
        print(f"  Warning: unsupported platform {system} for recording")
        return None

    cmd = [
        "ffmpeg",
        "-y",
        *input_args,
        "-t", str(duration),
        "-c:v", "libx264",
        "-crf", "28",
        "-preset", "fast",
        "-pix_fmt", "yuv420p",
        output_path,
    ]
    # Print the exact ffmpeg invocation so the user can debug rate /
    # format negotiation problems.
    print(f"    ffmpeg: {' '.join(cmd)}")
    try:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        if settle_time > 0:
            time.sleep(settle_time)
            if proc.poll() is not None:
                _, stderr = proc.communicate()
                err = stderr.decode(errors="replace").strip().split("\n")
                tail = "\n".join(err[-5:]) if err else "(no stderr)"
                print(f"    ffmpeg exited during settle:\n{tail}")
                return None
        return proc
    except FileNotFoundError:
        return None


def finish_recording(proc, duration=VIDEO_DURATION):
    """
    Wait for a background ffmpeg recording to finish.
    Returns (success, error_message).
    """
    if proc is None:
        return False, "ffmpeg not found in PATH"
    try:
        _, stderr = proc.communicate(timeout=duration + 15)
        if proc.returncode == 0:
            return True, ""
        err_lines = stderr.decode(errors="replace").strip().split("\n")
        return False, err_lines[-1] if err_lines else "unknown error"
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()
        return False, "Video recording timed out"


def student_name_from_zip(zip_name):
    """
    Extract a student identifier from the zip filename.
    Strips common lab prefixes.
    """
    base = os.path.splitext(zip_name)[0]
    for prefix in ("Lab_1_", "Lab 1_", "lab1_", "lab_1_",
                   "Lab_2_", "Lab 2_", "lab2_", "lab_2_",
                   "Lab_3_", "Lab 3_", "lab3_", "lab_3_"):
        if base.startswith(prefix):
            return base[len(prefix):]
    return base
