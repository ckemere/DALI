"""
Makefile Generator for DALI - TI MSPM0 Edition

Generates Makefiles that match CCS build settings for proper compilation.
"""

import os

# TI Toolchain paths - these should be configured per installation
TI_COMPILER_ROOT = os.environ.get('TI_COMPILER_ROOT', '/opt/ti/ccs/tools/compiler/ti-cgt-armllvm_4.0.4.LTS')
TI_SDK_ROOT = os.environ.get('TI_SDK_ROOT', '/opt/ti/mspm0_sdk_2_09_00_01')

# Device-specific settings
DEVICE_NAME = 'MSPM0G3507'
DEVICE_FAMILY = 'mspm0g1x0x_g3x0x'  # For library path

# Compiler and flags (from CCS)
CC = f'{TI_COMPILER_ROOT}/bin/tiarmclang'
CFLAGS = [
    '-march=thumbv6m',
    '-mcpu=cortex-m0plus',
    '-mfloat-abi=soft',
    '-mlittle-endian',
    '-mthumb',
    '-Og',  # Optimize for debug (students debugging)
    f'-D__{DEVICE_NAME}__',
    '-g',  # Debug symbols
    f'-I{TI_SDK_ROOT}/source',  # SDK headers
]

# Linker flags
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
    f'-Wl,-i{TI_COMPILER_ROOT}/lib',  # Compiler library path
]

# Libraries to link
LIBRARIES = [
    f'{TI_SDK_ROOT}/source/ti/driverlib/lib/ticlang/m0p/{DEVICE_FAMILY}/driverlib.a'
]

def create_makefile_for_lab(build_dir, source_files, output_name='firmware'):
    """
    Create a Makefile that matches CCS build settings.
    
    Args:
        build_dir: Directory where Makefile will be created
        source_files: List of .c files to compile
        output_name: Name of output file (default: firmware)
    """
    
    # Filter to just .c files
    c_files = [f for f in source_files if f.endswith('.c')]
    
    # Object files
    obj_files = [f.replace('.c', '.o') for f in c_files]
    
    # Linker command file (must be present in build_dir or template_files)
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
    
    # Write Makefile
    makefile_path = os.path.join(build_dir, 'Makefile')
    with open(makefile_path, 'w') as f:
        f.write(makefile_content)
    
    return makefile_path


def ensure_linker_script(build_dir, template_dir):
    """
    Ensure the linker command file (.cmd) is present.
    
    The .cmd file is critical for linking - it defines memory layout.
    Copy from template if not present in build directory.
    """
    cmd_filename = f'{DEVICE_NAME.lower()}.cmd'
    build_cmd = os.path.join(build_dir, cmd_filename)
    
    if not os.path.exists(build_cmd):
        # Try to copy from template
        template_cmd = os.path.join(template_dir, cmd_filename)
        if os.path.exists(template_cmd):
            import shutil
            shutil.copy(template_cmd, build_cmd)
            return True
        else:
            # Critical error - can't compile without linker script
            raise FileNotFoundError(
                f"Linker script {cmd_filename} not found in build directory or templates. "
                f"This file is required for linking. Please add it to template_files/{template_dir}/"
            )
    
    return True


def verify_toolchain():
    """
    Verify that TI toolchain is installed and accessible.
    
    Returns:
        tuple: (success: bool, message: str)
    """
    # Check compiler exists
    if not os.path.exists(CC):
        return False, f"Compiler not found at {CC}. Set TI_COMPILER_ROOT environment variable."
    
    # Check SDK exists
    if not os.path.exists(TI_SDK_ROOT):
        return False, f"SDK not found at {TI_SDK_ROOT}. Set TI_SDK_ROOT environment variable."
    
    # Check driverlib exists
    driverlib_path = LIBRARIES[0]
    if not os.path.exists(driverlib_path):
        return False, f"Driver library not found at {driverlib_path}. Check SDK installation."
    
    return True, "Toolchain verified successfully"


def get_compilation_command(build_dir, verbose=False):
    """
    Get the actual compilation command that will be run.
    Useful for debugging.
    """
    return f"make -C {build_dir} {'VERBOSE=1' if verbose else ''}"


# Example usage for integration with compile_queue.py
if __name__ == '__main__':
    # Test the Makefile generator
    import tempfile
    
    test_dir = tempfile.mkdtemp()
    
    # Example source files from Lab 3
    sources = [
        'hw_interface.c',
        'lab3.c', 
        'startup_mspm0g350x_ticlang.c',
        'state_machine_logic.c'
    ]
    
    print("Testing Makefile generation...")
    print(f"Test directory: {test_dir}")
    
    # Verify toolchain
    success, msg = verify_toolchain()
    print(f"\nToolchain check: {msg}")
    
    if success:
        # Create Makefile
        makefile = create_makefile_for_lab(test_dir, sources, 'Lab3')
        print(f"\n✓ Makefile created: {makefile}")
        
        # Show content
        print("\n--- Generated Makefile ---")
        with open(makefile, 'r') as f:
            print(f.read())
    else:
        print("\n✗ Cannot generate Makefile - toolchain not configured")
        print("\nSet environment variables:")
        print("  export TI_COMPILER_ROOT=/path/to/ti/ccs/tools/compiler/ti-cgt-armllvm_X.X.X")
        print("  export TI_SDK_ROOT=/path/to/ti/mspm0_sdk_X_XX_XX_XX")
