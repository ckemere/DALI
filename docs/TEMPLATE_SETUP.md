# Template Files - Installation Guide

## You Need These 4 HTML Templates

Save these files to your `templates/` directory:

1. **login_api.html** - Student login page
2. **home_api.html** - Assignment list
3. **assignment_complete.html** - File upload interface (with revert buttons)
4. **view_file.html** - Code viewer with syntax highlighting

## Installation

### Option 1: Download from Above
Click each file link and save to your `templates/` folder.

### Option 2: File Naming
**Important:** Rename `assignment_complete.html` to `assignment_api.html` in your templates folder.

The app looks for `assignment_api.html`, so either:
- Rename the file, OR
- Update line in `app_api_complete.py`:
  ```python
  # Change this line (around line 280):
  return render_template('assignment_api.html',
  # To:
  return render_template('assignment_complete.html',
  ```

## Directory Structure

Your final structure should be:

```
your-project/
├── app_api_complete.py
├── requirements_api.txt
├── .env
├── templates/                    ← Create this folder
│   ├── login_api.html            ← Download these 4 files
│   ├── home_api.html
│   ├── assignment_api.html       ← (renamed from assignment_complete.html)
│   └── view_file.html
└── template_files/
    └── lab3/
        ├── hw_interface.c
        ├── hw_interface.h
        ├── lab3.c
        ├── startup_mspm0g350x_ticlang.c
        ├── state_machine_logic.c
        └── state_machine_logic.h
```

## Quick Setup Commands

```bash
# Create templates directory
mkdir -p templates

# Download the 4 template files to templates/
# (Then rename assignment_complete.html → assignment_api.html)

# Or if you keep the filename, update the app:
# In app_api_complete.py, line ~280, change:
#   'assignment_api.html' → 'assignment_complete.html'
```

## Verify

Check you have all files:
```bash
ls templates/
# Should show:
# assignment_api.html
# home_api.html
# login_api.html
# view_file.html
```

## What Each Template Does

**login_api.html**
- Purple gradient design
- Student enters Canvas ID + Name
- Clean, modern interface

**home_api.html**
- Lists all assignments from Canvas
- Shows due dates and points
- Click to go to upload page

**assignment_api.html** (the main one!)
- Shows all code files (6 for Lab 3)
- "View Template" / "View My Code" buttons
- "Upload" / "Replace" buttons
- "🔄 Revert" buttons (NEW!)
- Writeup upload section
- "Submit to Canvas" button
- Real-time upload feedback

**view_file.html**
- Syntax-highlighted code viewer
- Opens in new tab
- Works for templates and student code

## Testing

After installing templates, test each page:

1. **Login:** `http://localhost:5000/login`
   - Should show purple login page
   
2. **Home:** (after login) `http://localhost:5000/`
   - Should show assignment list
   
3. **Assignment:** Click any assignment
   - Should show file upload interface
   - All buttons should be visible
   
4. **View:** Click "View Template"
   - Should open code in new tab
   - Should have syntax highlighting

## Common Issues

**"TemplateNotFound: assignment_api.html"**
→ Rename `assignment_complete.html` to `assignment_api.html`

**"TemplateNotFound: login_api.html"**
→ Make sure files are in `templates/` folder, not `template_files/`

**No styling / looks broken**
→ Check that entire HTML file was saved, including `<style>` section

## All Set!

Once you have all 4 templates in the `templates/` folder, the app is ready to run!
