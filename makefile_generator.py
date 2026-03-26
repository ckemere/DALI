"""
Makefile Generator for DALI - TI MSPM0 Edition

Thin wrapper — canonical implementation is in assess/build.py.
"""

# Re-export everything so existing consumers (compile_queue.py, etc.) work.
from assess.build import (  # noqa: F401
    TI_COMPILER_ROOT,
    TI_SDK_ROOT,
    DEVICE_NAME,
    DEVICE_FAMILY,
    CC,
    CFLAGS,
    LDFLAGS,
    LIBRARIES,
    verify_toolchain,
    create_makefile_for_lab,
    ensure_linker_script,
    get_compilation_command,
)


if __name__ == '__main__':
    import tempfile

    test_dir = tempfile.mkdtemp()
    sources = ['hw_interface.c', 'lab3.c', 'startup_mspm0g350x_ticlang.c',
               'state_machine_logic.c']

    print("Testing Makefile generation...")
    print(f"Test directory: {test_dir}")

    success, msg = verify_toolchain()
    print(f"\nToolchain check: {msg}")

    if success:
        makefile = create_makefile_for_lab(test_dir, sources, 'Lab3')
        print(f"\nMakefile created: {makefile}")
        print("\n--- Generated Makefile ---")
        with open(makefile, 'r') as f:
            print(f.read())
    else:
        print("\nCannot generate Makefile - toolchain not configured")
        print("\nSet environment variables:")
        print("  export TI_COMPILER_ROOT=/path/to/ti/ccs/tools/compiler/ti-cgt-armllvm_X.X.X")
        print("  export TI_SDK_ROOT=/path/to/ti/mspm0_sdk_X_XX_XX_XX")
