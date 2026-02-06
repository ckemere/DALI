#!/usr/bin/env python3
"""
Test Makefile Generator
Quick script to test if makefile_generator.py works
"""

import sys
import os

# Make sure we can import from current directory
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from makefile_generator import (
    create_makefile_for_lab,
    verify_toolchain,
    ensure_linker_script
)

print("=" * 50)
print("Testing DALI Makefile Generator")
print("=" * 50)
print()

# Step 1: Verify toolchain
print("Step 1: Verifying TI Toolchain...")
print("-" * 50)
success, msg = verify_toolchain()
print(msg)
print()

if not success:
    print("❌ Toolchain not configured!")
    print()
    print("Set environment variables:")
    print("  export TI_COMPILER_ROOT=/opt/ti/ti-cgt-armllvm_4.0.4.LTS")
    print("  export TI_SDK_ROOT=/opt/ti/mspm0_sdk_2_09_00_01")
    print()
    print("Or source ti_env.sh:")
    print("  source ti_env.sh")
    sys.exit(1)

# Step 2: Create test directory
print("Step 2: Creating test directory...")
print("-" * 50)

import tempfile
test_dir = tempfile.mkdtemp(prefix="dali_test_")
print(f"Test directory: {test_dir}")
print()

# Step 3: Generate Makefile
print("Step 3: Generating Makefile...")
print("-" * 50)

# Lab 3 source files
sources = [
    'hw_interface.c',
    'lab3.c',
    'startup_mspm0g350x_ticlang.c',
    'state_machine_logic.c'
]

try:
    makefile_path = create_makefile_for_lab(test_dir, sources, 'Lab3')
    print(f"✓ Makefile created: {makefile_path}")
    print()
except Exception as e:
    print(f"❌ Error creating Makefile: {e}")
    sys.exit(1)

# Step 4: Show the Makefile
print("Step 4: Generated Makefile Contents")
print("=" * 50)

with open(makefile_path, 'r') as f:
    print(f.read())

print("=" * 50)
print()

# Step 5: Summary
print("✓ Test Complete!")
print()
print("To test compilation:")
print(f"  1. Copy template files to {test_dir}")
print(f"  2. cd {test_dir}")
print(f"  3. make clean all")
print()
