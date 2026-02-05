# ⏰ DALI

### Dynamic Assignment Lab Interface

> *Surreally simple submissions for embedded systems*

---

DALI is a modern lab submission system designed for embedded systems courses. Students upload code, test compilation in real-time, and submit directly to Canvas—all while watching their position in the compilation queue melt away.

[![Python 3.8+](https://img.shields.io/badge/python-3.8+-blue.svg)](https://www.python.org/downloads/)
[![Flask](https://img.shields.io/badge/flask-2.3.0-green.svg)](https://flask.palletsprojects.com/)
[![Redis](https://img.shields.io/badge/redis-required-red.svg)](https://redis.io/)

---

## 🎨 Why DALI?

Named after Salvador Dalí, the famous surrealist painter, DALI brings artistic precision to the mundane world of assignment submissions. 
Just as Dalí bent time with his melting clocks, DALI bends the traditional submission workflow—making it fluid, intuitive, and dare we say... surreal.

**Key Philosophy:** Lab submissions should be as smooth as melting butter, not as painful as debugging assembly at 3 AM.

---

## ✨ Features

### For Students

- **🔨 Pre-Submission Compilation Testing**
  - Test your code before submitting
  - See compilation errors in real-time
  - Fix issues before they cost you points

- **⏳ Real-Time Queue Visibility**
  - See your position in the compilation queue
  - Know exactly how long you'll wait
  - Cancel jobs if you change your mind

- **📦 Template-Based Development**
  - Start with instructor-provided templates
  - Override only the files you need
  - Revert to templates with one click

- **✅ Canvas Integration**
  - Submit directly to Canvas gradebook
  - Automatic zip file creation
  - Instant submission confirmation

### For Instructors

- **👀 Admin Dashboard**
  - Monitor compilation queue in real-time
  - See student netIDs (not Canvas IDs)
  - Track who's submitting what
  - Auto-refreshing live view

- **⚙️ Multi-Core Compilation**
  - Parallel processing with configurable workers
  - Handle deadline rushes with ease
  - 50 students? Done in 18 seconds.

- **🎯 Flexible Lab Configuration**
  - Define required files per assignment
  - Map Canvas assignments to lab templates
  - Support multiple labs simultaneously

---

## 🏗️ Architecture

```
Student uploads code
        ↓
  Redis Queue (⏰ time melts here)
        ↓
  16 Parallel Workers
        ↓
  Ti-ArmClang Compilation
        ↓
  Results + Canvas Upload
```

**No Docker needed!** Just a beefy server, Redis, and your compiler of choice.

---

## 🚀 Quick Start

### Prerequisites

- Python 3.8+
- Redis server
- Ti-ArmClang (or your embedded compiler)
- Canvas LMS with API access

### Installation

```bash
# Clone the repository
git clone https://github.com/yourusername/dali.git
cd dali

# Create virtual environment
python3 -m venv venv
source venv/bin/activate  # On Windows: venv\Scripts\activate

# Install dependencies
pip install -r requirements.txt

# Install Redis
# Ubuntu/Debian:
sudo apt install redis-server

# macOS:
brew install redis
```

### Configuration

```bash
# Create .env file
cat > .env << EOF
FLASK_SECRET_KEY=$(python3 -c "import secrets; print(secrets.token_hex(32))")
CANVAS_API_TOKEN=your_canvas_api_token_here
CANVAS_BASE_URL=https://canvas.youruniversity.edu
COURSE_ID=your_course_id
GRADEBOOK_CSV_PATH=gradebook.csv
ADMIN_PASSWORD=$(python3 -c "import secrets; print(secrets.token_urlsafe(16))")
EOF

# Edit .env with your actual credentials
nano .env
```

### Set Up Templates

```bash
# Create template directory structure
mkdir -p template_files/lab3

# Copy your lab templates
cp /path/to/your/templates/*.c template_files/lab3/
cp /path/to/your/templates/*.h template_files/lab3/
```

### Download Canvas Gradebook

1. Go to Canvas → Gradebook → Export
2. Save as `gradebook.csv` in project root
3. This maps Canvas IDs to student netIDs

### Run

```bash
# Start Redis
redis-server &

# Start DALI
python3 app_api_complete.py

# Visit http://localhost:5000
```

---

## 📚 Usage

### Student Workflow

1. **Login** with Canvas student ID and name
2. **Select assignment** from the list
3. **Upload modified files** (or use templates)
4. **Test compilation:**
   - Click "🔨 Test Compilation"
   - See queue position: "⏳ Position #3, wait ~8 seconds"
   - View compilation results
5. **Upload writeup** (TXT or PDF)
6. **Submit to Canvas** when ready

### Instructor Workflow

1. **Set up lab templates** in `template_files/`
2. **Configure lab** in `LAB_CONFIGS` (see Configuration section)
3. **Create Canvas assignment** with matching name
4. **Monitor submissions:**
   - Go to `/admin/compile-queue`
   - Enter admin password
   - Watch real-time queue
5. **Download submissions** from Canvas
6. **Grade** using your preferred method

---

## ⚙️ Configuration

### Adding a New Lab

Edit `app_api_complete.py`:

```python
LAB_CONFIGS = {
    'lab3': {
        'template_dir': 'lab3',
        'code_files': [
            'hw_interface.c',
            'hw_interface.h',
            'lab3.c',
            'startup_mspm0g350x_ticlang.c',
            'state_machine_logic.c',
            'state_machine_logic.h'
        ],
        'writeup_files': ['writeup.txt', 'writeup.pdf'],
        'editable_files': [
            'hw_interface.c',
            'state_machine_logic.c',
            'lab3.c'
        ]
    },
    'lab4': {  # Add your new lab here
        'template_dir': 'lab4',
        'code_files': ['main.c', 'functions.c', 'functions.h'],
        'writeup_files': ['writeup.txt', 'writeup.pdf'],
        'editable_files': ['main.c', 'functions.c']
    }
}
```

Then create templates:
```bash
mkdir -p template_files/lab4
cp your_templates/* template_files/lab4/
```

### Adjusting Worker Count

In `compile_queue.py`:

```python
compile_queue = CompilationQueue(max_workers=16)  # Adjust based on CPU cores
```

**Guidelines:**
- 8 cores → 8 workers
- 16 cores → 16 workers
- 32 cores → 32 workers

---

## 🎯 Performance

### Tested Scenarios

**50 students, deadline rush:**
- All jobs queued: <100ms
- All completed: ~18 seconds
- Average wait per student: <2 seconds

**100 students:**
- All completed: ~32 seconds
- Average wait: ~16 seconds

### Server Requirements

| Students | Recommended Server | Monthly Cost |
|----------|-------------------|--------------|
| 50       | 16 cores, 32GB RAM | $80-120 |
| 100      | 32 cores, 64GB RAM | $160-240 |
| 200      | 64 cores, 128GB RAM | $320-480 |

---

## 🖼️ Screenshots

### Student View

```
┌────────────────────────────────────┐
│  Lab 3 - Embedded Clock            │
├────────────────────────────────────┤
│  Code Files:                       │
│  ✓ hw_interface.c      [Replace]   │
│  📄 lab3.c             [Upload]    │
│  ✓ state_machine.c     [Revert]    │
│                                    │
│  Writeup:                          │
│  ✓ writeup.pdf         [Replace]   │
│                                    │
│  [🔨 Test Compilation]             │
│  [Submit to Canvas]                │
└────────────────────────────────────┘
```

### Compilation Queue

```
┌────────────────────────────────────┐
│  ⏳ In Queue                        │
│  Position: #3                      │
│  Estimated wait: 8 seconds         │
│  [Cancel]                          │
└────────────────────────────────────┘
```

### Admin Dashboard

```
┌──────────────────────────────────────────────────┐
│  🔨 Compilation Queue Dashboard                  │
├──────────────────────────────────────────────────┤
│  Workers: 16  │  Queued: 5  │  Compiling: 3     │
├──────┬────────┬──────────┬──────────┬───────────┤
│ Pos  │ NetID  │ Student  │ Lab      │ Status    │
├──────┼────────┼──────────┼──────────┼───────────┤
│  #1  │ jd123  │ John D   │ Lab 3    │ ⏳ Queued │
│  #2  │ js456  │ Jane S   │ Lab 3    │ ⏳ Queued │
│  —   │ ba789  │ Bob A    │ Lab 3    │ ⚙️ Comp.  │
└──────┴────────┴──────────┴──────────┴───────────┘
```

---

## 🎓 Educational Use

DALI was built for **ELEC 327: Embedded Systems** at Rice University but can be adapted for any course that involves:
- Code submissions
- Compilation checking
- Template-based assignments
- Canvas LMS integration

### Courses That Could Use DALI

- Embedded Systems
- Computer Architecture
- Operating Systems
- Compilers
- Any course with C/C++/assembly code

---

## 🛠️ Development

### Project Structure

```
dali/
├── app_api_complete.py          # Main Flask application
├── compile_queue.py             # Queue management system
├── compile_routes.py            # Compilation endpoints
├── requirements.txt             # Python dependencies
├── .env                         # Configuration (not in git)
├── templates/                   # HTML templates
│   ├── login_api.html
│   ├── home_api.html
│   ├── assignment_complete.html
│   ├── view_file.html
│   ├── admin_queue.html
│   └── admin_login.html
├── template_files/              # Lab templates
│   ├── lab3/
│   ├── lab4/
│   └── lab5/
└── uploads/                     # Student submissions
    └── student_{id}/
        └── assignment_{id}/
```

### Running Tests

```bash
# Start Redis
redis-server &

# Run the app
python3 app_api_complete.py

# In another terminal, test the API
curl http://localhost:5000/

# Test compilation queue
python3 -c "from compile_queue import CompilationQueue; q = CompilationQueue(); print('Queue OK!')"
```

### Contributing

Contributions welcome! Please:

1. Fork the repository
2. Create a feature branch (`git checkout -b feature/amazing-feature`)
3. Commit your changes (`git commit -m 'Add amazing feature'`)
4. Push to the branch (`git push origin feature/amazing-feature`)
5. Open a Pull Request

---

## 🔒 Security

### Best Practices

- **Secret Keys:** Always use randomly generated secrets
- **HTTPS:** Required for production (Canvas API requirement)
- **Admin Password:** Use strong password, change regularly
- **API Tokens:** Never commit to git, use environment variables
- **Student Code:** Compiled but not executed (safe)

### Security Features

- Session-based authentication
- CSRF protection (built into Flask)
- File type validation
- Size limits (16MB max)
- Timeout protection (30s compilation limit)
- Isolated student directories

---

## 📖 Documentation

- **[Complete Setup Guide](COMPILE_QUEUE_SETUP.md)** - Full installation & configuration
- **[Feature Summary](COMPILATION_FEATURES_SUMMARY.md)** - Detailed feature breakdown
- **[Grade Options](GRADE_OPTIONS.md)** - How to configure grading
- **[Template Setup](TEMPLATE_SETUP.md)** - HTML template installation

---

## 🐛 Troubleshooting

### Common Issues

**"Connection refused" to Redis**
```bash
# Start Redis
sudo systemctl start redis-server
```

**"tiarmclang: command not found"**
```bash
# Add compiler to PATH
export PATH="/opt/ti-armclang/bin:$PATH"
```

**NetIDs showing as "canvas_106586"**
```bash
# Make sure gradebook.csv is loaded
# Check GRADEBOOK_CSV_PATH in .env
```

**Compilation timeout**
```bash
# Increase timeout in compile_queue.py
# Change timeout=30 to timeout=60
```
---

## 📊 Roadmap

### Current Version (v1.0)
- ✅ Template-based file management
- ✅ Canvas integration
- ✅ Compilation queue with position tracking
- ✅ Admin dashboard
- ✅ Job cancellation

### Planned Features (v2.0)
- [ ] Compilation history per student
- [ ] Email notifications on completion
- [ ] Advanced error parsing (line number links)
- [ ] Plagiarism detection integration
- [ ] TA grading interface
- [ ] Multiple course support
- [ ] LTI 1.3 integration (seamless Canvas launch)

### Future Possibilities
- [ ] Real-time collaboration (pair programming)
- [ ] Code review interface
- [ ] Automated testing framework
- [ ] Git integration
- [ ] Docker support (for other compilers)

---

## 🙏 Acknowledgments

- **Salvador Dalí** - For the surrealist inspiration
- **Canvas LMS** - For the platform we integrate with
- **Texas Instruments** - For Ti-ArmClang compiler
- **Rice University** - For being the testing ground
- **ELEC 327 Students** - For being the beta testers

---

## 📄 License

This project is licensed under the GPLv3 License - see the [LICENSE](LICENSE) file for details.


---

## 📧 Contact

**Project Maintainer:** Caleb Kemere  
**Institution:** Rice University  

**Issues:** [GitHub Issues](https://github.com/ckemere/dali/issues)  
**Discussions:** [GitHub Discussions](https://github.com/ckemere/dali/discussions)

---

## ⭐ Star History

If DALI has made your life easier, consider giving it a star! ⭐

---

<div align="center">

**⏰ DALI - Where time melts and submissions flow**

*Surreally simple submissions for embedded systems*

Made with 💜 at Rice University

[Documentation](docs/) • [Report Bug](issues/) • [Request Feature](issues/)

</div>
