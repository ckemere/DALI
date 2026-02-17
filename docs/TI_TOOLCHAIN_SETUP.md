# TI Toolchain Setup for DALI

## Required Components

DALI needs these TI components installed on the compilation server:

### 1. **TI ARM LLVM Compiler** (ti-cgt-armllvm)
- Version: 4.0.4.LTS (or newer)
- Mac path: `/Applications/ti/ccs2040/ccs/tools/compiler/ti-cgt-armllvm_4.0.4.LTS`
- Linux path: `/opt/ti/ccs/tools/compiler/ti-cgt-armllvm_4.0.4.LTS`

### 2. **MSPM0 SDK**
- Version: 2.09.00.01 (or newer)
- Mac path: `/Users/ckemere/ti/mspm0_sdk_2_09_00_01`
- Linux path: `/opt/ti/mspm0_sdk_2_09_00_01`

### 3. **Linker Script** (Critical!)
- File: `mspm0g3507.cmd`
- This defines the memory layout for the MSPM0G3507
- Must be present in `template_files/` for each lab

### 4. **Driver Library**
- File: `driverlib.a`
- Path: `<SDK>/source/ti/driverlib/lib/ticlang/m0p/mspm0g1x0x_g3x0x/driverlib.a`

---

## Installation on Linux Server

### Option 1: Install Full CCS (Easiest)

```bash
# Download CCS for Linux from TI
# https://www.ti.com/tool/CCSTUDIO

# Install (graphical installer)
chmod +x CCS*.run
./CCS*.run

# Toolchain will be at:
# /opt/ti/ccs/tools/compiler/ti-cgt-armllvm_X.X.X
```

### Option 2: Standalone Compiler + SDK (Lighter)

```bash
# 1. Download ARM LLVM Compiler
# https://www.ti.com/tool/download/ARM-CGT-CLANG

# Extract to /opt/ti
sudo mkdir -p /opt/ti
sudo tar -xzf ti-cgt-armllvm_4.0.4.LTS-linux-x64.tar.gz -C /opt/ti/

# 2. Download MSPM0 SDK
# https://www.ti.com/tool/MSPM0-SDK

# Install
chmod +x mspm0_sdk_*.run
sudo ./mspm0_sdk_*.run --prefix /opt/ti
```

### Option 3: Copy from Your Mac (Quick Test)

```bash
# On your Mac, compress the toolchain
cd /Applications/ti/ccs2040/ccs/tools/compiler/
tar -czf ti-cgt-armllvm.tar.gz ti-cgt-armllvm_4.0.4.LTS

cd ~/ti
tar -czf mspm0_sdk.tar.gz mspm0_sdk_2_09_00_01

# Copy to Linux server
scp ti-cgt-armllvm.tar.gz user@server:/tmp/
scp mspm0_sdk.tar.gz user@server:/tmp/

# On Linux server
sudo mkdir -p /opt/ti
cd /opt/ti
sudo tar -xzf /tmp/ti-cgt-armllvm.tar.gz
sudo tar -xzf /tmp/mspm0_sdk.tar.gz
```

---

## Configuration

### Set Environment Variables

Add to `/etc/environment` (system-wide) or `~/.bashrc` (per-user):

```bash
# TI Toolchain paths
export TI_COMPILER_ROOT=/opt/ti/ti-cgt-armllvm_4.0.4.LTS
export TI_SDK_ROOT=/opt/ti/mspm0_sdk_2_09_00_01

# Add compiler to PATH
export PATH=$TI_COMPILER_ROOT/bin:$PATH
```

Apply changes:
```bash
source ~/.bashrc
# or for system-wide:
sudo systemctl restart your-dali-service
```

### Verify Installation

```bash
# Check compiler
which tiarmclang
# Should output: /opt/ti/ti-cgt-armllvm_4.0.4.LTS/bin/tiarmclang

tiarmclang --version
# Should show version 4.0.4 or newer

# Check SDK
ls $TI_SDK_ROOT/source/ti/driverlib/lib/ticlang/m0p/mspm0g1x0x_g3x0x/driverlib.a
# Should exist

# Test compilation (in DALI)
python3 -c "from makefile_generator import verify_toolchain; print(verify_toolchain())"
# Should output: (True, 'Toolchain verified successfully')
```

---

## Add Linker Script to Templates

### Extract from CCS Project

On your Mac (in CCS):
1. Right-click your Lab 3 project
2. Show in Finder
3. Copy `mspm0g3507.cmd` file

### Add to DALI

```bash
# On your DALI server
cd /path/to/dali
cp mspm0g3507.cmd template_files/lab3/

# Verify
ls -l template_files/lab3/mspm0g3507.cmd
```

**Important:** Every lab template directory needs this `.cmd` file!

---

## Required Files Per Lab

For each lab in `template_files/labX/`, you need:

```
template_files/lab3/
├── hw_interface.c
├── hw_interface.h
├── lab3.c
├── startup_mspm0g350x_ticlang.c
├── state_machine_logic.c
├── state_machine_logic.h
└── mspm0g3507.cmd         # ⚠️ CRITICAL - linker script!
```

`makefile_generator.py` auto-discovers all `.c` and `.h` files and locates
the `.cmd` file. No manual Makefile editing is needed when adding labs.

---

## Troubleshooting

### "tiarmclang: command not found"

**Problem:** Compiler not in PATH  
**Fix:**
```bash
export PATH=/opt/ti/ti-cgt-armllvm_4.0.4.LTS/bin:$PATH
```

### "cannot find -ldriverlib.a"

**Problem:** SDK not installed or wrong path  
**Fix:**
```bash
# Verify SDK location
ls /opt/ti/mspm0_sdk_*/source/ti/driverlib/lib/ticlang/m0p/mspm0g1x0x_g3x0x/driverlib.a

# Update TI_SDK_ROOT if needed
export TI_SDK_ROOT=/opt/ti/mspm0_sdk_2_09_00_01
```

### "mspm0g3507.cmd: No such file"

**Problem:** Linker script missing from lab template directory  
**Fix:**
```bash
# Copy from your CCS project to each lab template
cp /path/to/ccs/project/mspm0g3507.cmd template_files/lab3/
cp /path/to/ccs/project/mspm0g3507.cmd template_files/lab4/
# etc.
```

### "undefined reference to __TI_*"

**Problem:** Compiler library path not found  
**Fix:**
```bash
# Verify TI_COMPILER_ROOT is set correctly
export TI_COMPILER_ROOT=/opt/ti/ti-cgt-armllvm_4.0.4.LTS
ls $TI_COMPILER_ROOT/lib
```

### Compilation works in CCS but not DALI

**Debug steps:**
```bash
# 1. Run a manual test compilation
mkdir -p /tmp/test_compile
cp template_files/lab3/* /tmp/test_compile/
cd /tmp/test_compile

python3 -c "
from makefile_generator import create_makefile_for_lab
sources = ['hw_interface.c', 'lab3.c', 'startup_mspm0g350x_ticlang.c', 'state_machine_logic.c']
create_makefile_for_lab('.', sources, 'test')
"
make clean all

# 2. Run make with verbose output to see exact flags
make clean all VERBOSE=1

# 3. Check environment variables are visible to the worker process
env | grep TI
```

---

## Compiler Flags

Flags used by `makefile_generator.py`, matching a stock CCS Lab 3 build:

| Flag | Purpose |
|------|---------|
| `-march=thumbv6m` | ARM Thumb-1 instruction set |
| `-mcpu=cortex-m0plus` | Target Cortex-M0+ processor |
| `-mfloat-abi=soft` | Software floating point |
| `-mlittle-endian` | Little-endian byte order |
| `-mthumb` | Use Thumb mode |
| `-Og` | Optimize for debugging |
| `-D__MSPM0G3507__` | Define device macro |
| `-g` | Include debug symbols |

---

## Performance Notes

- Clean build: ~5 seconds per student
- Incremental (one changed file): ~2 seconds
- With 8 workers: 50 students ≈ 35 seconds total
- With 16 workers: 50 students ≈ 18 seconds total
