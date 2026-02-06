from flask import Flask, request, render_template, jsonify, session, redirect, url_for, send_file, flash
import os
from werkzeug.utils import secure_filename
import requests
from datetime import datetime
import json
import zipfile
import io
import shutil
import logging

app = Flask(__name__)
app.secret_key = os.environ.get('FLASK_SECRET_KEY', 'dev-secret-key-change-for-production')

# Configuration
UPLOAD_FOLDER = 'uploads'
TEMPLATE_FOLDER = 'template_files'
ALLOWED_CODE_EXTENSIONS = {'c', 'h'}
ALLOWED_DOC_EXTENSIONS = {'txt', 'pdf'}
os.makedirs(UPLOAD_FOLDER, exist_ok=True)
os.makedirs(TEMPLATE_FOLDER, exist_ok=True)

app.config['UPLOAD_FOLDER'] = UPLOAD_FOLDER
app.config['MAX_CONTENT_LENGTH'] = 16 * 1024 * 1024  # 16MB max

# Canvas API Configuration
CANVAS_API_TOKEN = os.environ.get('CANVAS_API_TOKEN', 'YOUR_TOKEN_HERE')
CANVAS_BASE_URL = os.environ.get('CANVAS_BASE_URL', 'https://canvas.rice.edu')
COURSE_ID = os.environ.get('COURSE_ID', 'YOUR_COURSE_ID')

# Lab Configuration - maps assignment names to template directories and required files
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
        'writeup_files': ['writeup.txt', 'writeup.pdf'],  # Students choose one
        'editable_files': [  # Files students typically modify
            'hw_interface.c',
            'state_machine_logic.c',
            'lab3.c'
        ]
    },
    # Add more labs here as needed
    # 'lab4': { ... },
}

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s %(levelname)s: %(message)s',
    handlers=[
        logging.FileHandler('app.log'),
        logging.StreamHandler()
    ]
)

def allowed_file(filename, allowed_extensions):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in allowed_extensions

def get_lab_config(assignment_name):
    """Get lab configuration from assignment name"""
    # Try direct match first
    if assignment_name in LAB_CONFIGS:
        return LAB_CONFIGS[assignment_name]
    
    # Try to match "Lab 3" -> "lab3"
    normalized = assignment_name.lower().replace(' ', '')
    if normalized in LAB_CONFIGS:
        return LAB_CONFIGS[normalized]
    
    return None

def get_submission_folder(student_id, assignment_id):
    """Create a unique folder for each student/assignment combination"""
    folder = os.path.join(UPLOAD_FOLDER, f"student_{student_id}", f"assignment_{assignment_id}")
    os.makedirs(folder, exist_ok=True)
    return folder

def get_template_file_path(lab_name, filename):
    """Get path to a template file"""
    return os.path.join(TEMPLATE_FOLDER, lab_name, filename)

def canvas_api_request(endpoint, method='GET', data=None, files=None):
    """Make a request to Canvas API"""
    url = f"{CANVAS_BASE_URL}/api/v1/{endpoint}"
    headers = {'Authorization': f'Bearer {CANVAS_API_TOKEN}'}
    
    try:
        if method == 'GET':
            response = requests.get(url, headers=headers, timeout=30)
        elif method == 'POST':
            if files:
                response = requests.post(url, headers=headers, files=files, data=data, timeout=30)
            else:
                headers['Content-Type'] = 'application/json'
                response = requests.post(url, headers=headers, json=data, timeout=30)
        elif method == 'PUT':
            headers['Content-Type'] = 'application/json'
            response = requests.put(url, headers=headers, json=data, timeout=30)
        
        response.raise_for_status()
        return response.json()
    except requests.Timeout:
        logging.error(f"Canvas API timeout: {endpoint}")
        raise Exception("Canvas is taking too long to respond. Please try again.")
    except requests.ConnectionError:
        logging.error(f"Canvas API connection error: {endpoint}")
        raise Exception("Cannot connect to Canvas. Please check your internet connection.")
    except requests.HTTPError as e:
        logging.error(f"Canvas API HTTP error: {e.response.status_code} - {endpoint}")
        raise Exception(f"Canvas API error: {e.response.status_code}")

def upload_file_as_comment(student_id, assignment_id, file_path, filename):
    """Upload a file and attach it to a submission comment"""
    try:
        # Step 1: Request upload URL for submission comment file
        endpoint = f'courses/{COURSE_ID}/assignments/{assignment_id}/submissions/{student_id}/comments/files'
        data = {
            'name': filename,
            'size': os.path.getsize(file_path),
            'content_type': 'application/zip'
        }
        
        upload_info = canvas_api_request(endpoint, method='POST', data=data)
        upload_url = upload_info['upload_url']
        upload_params = upload_info['upload_params']
        
        # Step 2: Upload file to the provided URL (S3 or Canvas storage)
        with open(file_path, 'rb') as f:
            files = {'file': (filename, f, 'application/zip')}
            upload_response = requests.post(upload_url, data=upload_params, files=files, timeout=60)
            upload_response.raise_for_status()
        
        # Step 3: Confirm the upload and get file ID
        if 'Location' in upload_response.headers:
            confirm_url = upload_response.headers['Location']
            headers = {'Authorization': f'Bearer {CANVAS_API_TOKEN}'}
            confirm_response = requests.get(confirm_url, headers=headers, timeout=30)
            confirm_response.raise_for_status()
            file_data = confirm_response.json()
            file_id = file_data.get('id')
        else:
            # Try to parse response directly
            file_data = upload_response.json()
            file_id = file_data.get('id')
        
        if not file_id:
            raise Exception("Could not get file ID from Canvas response")
        
        return file_id
        
    except Exception as e:
        logging.error(f"File upload for comment failed: {str(e)}")
        raise

def submit_to_canvas_with_comment(student_id, assignment_id, file_id, filename):
    """Submit assignment with comment, file attachment, and nominal grade"""
    try:
        # Create submission with comment, file attachment, and grade
        endpoint = f'courses/{COURSE_ID}/assignments/{assignment_id}/submissions/{student_id}'
        data = {
            'comment': {
                'text_comment': f'Submitted via Lab Submission System. Zip file: {filename}',
                'file_ids': [file_id]
            },
            'submission': {
                'submission_type': 'online_upload',
                'submitted_at': datetime.utcnow().isoformat() + 'Z',
                'posted_grade': 'complete'  # or use a number like '1' or '100'
            }
        }
        
        result = canvas_api_request(endpoint, method='PUT', data=data)
        return True
        
    except Exception as e:
        logging.error(f"Submission with comment failed: {str(e)}")
        raise

def create_submission_zip(student_id, assignment_id, lab_config, lab_name):
    """Create a zip file with all required files (student uploads + templates)"""
    submission_folder = get_submission_folder(student_id, assignment_id)
    
    # Create zip in memory
    zip_buffer = io.BytesIO()
    
    with zipfile.ZipFile(zip_buffer, 'w', zipfile.ZIP_DEFLATED) as zip_file:
        # Add code files (student version or template)
        for filename in lab_config['code_files']:
            student_file = os.path.join(submission_folder, filename)
            
            if os.path.exists(student_file):
                # Use student's uploaded version
                zip_file.write(student_file, filename)
            else:
                # Use template version
                template_file = get_template_file_path(lab_name, filename)
                if os.path.exists(template_file):
                    zip_file.write(template_file, filename)
        
        # Add writeup if uploaded
        for writeup in lab_config['writeup_files']:
            student_writeup = os.path.join(submission_folder, writeup)
            if os.path.exists(student_writeup):
                zip_file.write(student_writeup, writeup)
                break  # Only include one writeup
    
    # Save to temp file for Canvas upload
    temp_zip_path = os.path.join(submission_folder, 'submission.zip')
    with open(temp_zip_path, 'wb') as f:
        f.write(zip_buffer.getvalue())
    
    return temp_zip_path

@app.route('/')
def home():
    """Landing page - student selects assignment"""
    if 'student_id' not in session:
        return redirect(url_for('login'))
    
    try:
        assignments = canvas_api_request(f'courses/{COURSE_ID}/assignments')
        return render_template('home_api.html', 
                             assignments=assignments,
                             student_name=session.get('student_name'))
    except Exception as e:
        logging.error(f"Failed to fetch assignments: {str(e)}")
        flash(f'Error loading assignments: {str(e)}', 'error')
        return render_template('home_api.html', assignments=[], student_name=session.get('student_name'))

@app.route('/login', methods=['GET', 'POST'])
def login():
    """Simple login - student enters their Canvas student ID"""
    if request.method == 'POST':
        student_id = request.form.get('student_id')
        student_name = request.form.get('student_name')
        
        if student_id and student_name:
            session['student_id'] = student_id
            session['student_name'] = student_name
            session.permanent = True
            return redirect(url_for('home'))
        else:
            flash('Please provide both Student ID and Name', 'error')
    
    return render_template('login_api.html')

@app.route('/logout')
def logout():
    """Clear session"""
    session.clear()
    return redirect(url_for('login'))

@app.route('/assignment/<assignment_id>')
def assignment(assignment_id):
    """Main interface for a specific assignment"""
    if 'student_id' not in session:
        return redirect(url_for('login'))
    
    student_id = session.get('student_id')
    student_name = session.get('student_name')
    
    try:
        # Get assignment details from Canvas
        assignment_data = canvas_api_request(f'courses/{COURSE_ID}/assignments/{assignment_id}')
        assignment_title = assignment_data.get('name', 'Assignment')
        
        # Get lab configuration
        lab_config = get_lab_config(assignment_title)
        
        if not lab_config:
            flash(f'No lab configuration found for "{assignment_title}". Contact instructor.', 'error')
            return redirect(url_for('home'))
        
        lab_name = assignment_title.lower().replace(' ', '')
        
        # Get current submission status
        submission_folder = get_submission_folder(student_id, assignment_id)
        uploaded_files = {}
        
        # Check code files
        for filename in lab_config['code_files']:
            filepath = os.path.join(submission_folder, filename)
            if os.path.exists(filepath):
                stat = os.stat(filepath)
                uploaded_files[filename] = {
                    'uploaded': True,
                    'size': stat.st_size,
                    'modified': datetime.fromtimestamp(stat.st_mtime).strftime('%Y-%m-%d %H:%M:%S'),
                    'is_code': True
                }
            else:
                uploaded_files[filename] = {
                    'uploaded': False,
                    'size': None,
                    'modified': None,
                    'is_code': True
                }
        
        # Check writeup files
        for filename in lab_config['writeup_files']:
            filepath = os.path.join(submission_folder, filename)
            if os.path.exists(filepath):
                stat = os.stat(filepath)
                uploaded_files[filename] = {
                    'uploaded': True,
                    'size': stat.st_size,
                    'modified': datetime.fromtimestamp(stat.st_mtime).strftime('%Y-%m-%d %H:%M:%S'),
                    'is_code': False
                }
        
        return render_template('assignment_api.html',
                             student_name=student_name,
                             assignment_id=assignment_id,
                             assignment_title=assignment_title,
                             lab_name=lab_name,
                             code_files=lab_config['code_files'],
                             writeup_files=lab_config['writeup_files'],
                             uploaded_files=uploaded_files)
    
    except Exception as e:
        logging.error(f"Error loading assignment {assignment_id}: {str(e)}")
        flash(f'Error loading assignment: {str(e)}', 'error')
        return redirect(url_for('home'))

@app.route('/upload/<assignment_id>/<filename>', methods=['POST'])
def upload_file(assignment_id, filename):
    """Handle file upload for a specific file"""
    if 'student_id' not in session:
        return jsonify({'error': 'Not authenticated'}), 403
    
    if 'file' not in request.files:
        return jsonify({'error': 'No file provided'}), 400
    
    file = request.files['file']
    
    if file.filename == '':
        return jsonify({'error': 'No file selected'}), 400
    
    # Check file extension
    is_code = allowed_file(filename, ALLOWED_CODE_EXTENSIONS)
    is_doc = allowed_file(filename, ALLOWED_DOC_EXTENSIONS)
    
    if not (is_code or is_doc):
        return jsonify({'error': 'Invalid file type'}), 400
    
    try:
        # Save to student's submission folder
        student_id = session.get('student_id')
        submission_folder = get_submission_folder(student_id, assignment_id)
        
        filepath = os.path.join(submission_folder, secure_filename(filename))
        file.save(filepath)
        
        stat = os.stat(filepath)
        logging.info(f"Student {student_id} uploaded {filename} for assignment {assignment_id}")
        
        return jsonify({
            'success': True,
            'filename': filename,
            'size': stat.st_size,
            'modified': datetime.fromtimestamp(stat.st_mtime).strftime('%Y-%m-%d %H:%M:%S')
        })
    except Exception as e:
        logging.error(f"Upload failed: {str(e)}")
        return jsonify({'error': str(e)}), 500

@app.route('/view/<assignment_id>/<filename>')
def view_file(assignment_id, filename):
    """View contents of an uploaded file"""
    if 'student_id' not in session:
        return "Not authenticated", 403
    
    student_id = session.get('student_id')
    submission_folder = get_submission_folder(student_id, assignment_id)
    filepath = os.path.join(submission_folder, filename)
    
    if not os.path.exists(filepath):
        return "File not uploaded yet", 404
    
    try:
        with open(filepath, 'r') as f:
            content = f.read()
        return render_template('view_file.html', filename=filename, content=content)
    except Exception as e:
        return f"Error reading file: {str(e)}", 500

@app.route('/view-template/<lab_name>/<filename>')
def view_template(lab_name, filename):
    """View contents of a template file"""
    if 'student_id' not in session:
        return "Not authenticated", 403
    
    template_path = get_template_file_path(lab_name, filename)
    
    if not os.path.exists(template_path):
        return "Template file not found", 404
    
    try:
        with open(template_path, 'r') as f:
            content = f.read()
        return render_template('view_file.html', filename=f"{filename} (Template)", content=content)
    except Exception as e:
        return f"Error reading template: {str(e)}", 500

@app.route('/revert/<assignment_id>/<filename>', methods=['POST'])
def revert_to_template(assignment_id, filename):
    """Revert a file back to template version"""
    if 'student_id' not in session:
        return jsonify({'error': 'Not authenticated'}), 403
    
    try:
        student_id = session.get('student_id')
        submission_folder = get_submission_folder(student_id, assignment_id)
        filepath = os.path.join(submission_folder, filename)
        
        # Delete student's uploaded version
        if os.path.exists(filepath):
            os.remove(filepath)
            logging.info(f"Student {student_id} reverted {filename} to template for assignment {assignment_id}")
        
        return jsonify({
            'success': True,
            'message': f'{filename} reverted to template version'
        })
    except Exception as e:
        logging.error(f"Revert failed: {str(e)}")
        return jsonify({'error': str(e)}), 500

@app.route('/submit/<assignment_id>', methods=['POST'])
def submit(assignment_id):
    """Submit the assignment to Canvas"""
    if 'student_id' not in session:
        return jsonify({'error': 'Not authenticated'}), 403
    
    student_id = session.get('student_id')
    student_name = session.get('student_name')
    
    try:
        # Get assignment info
        assignment_data = canvas_api_request(f'courses/{COURSE_ID}/assignments/{assignment_id}')
        assignment_title = assignment_data.get('name', 'Assignment')
        lab_config = get_lab_config(assignment_title)
        
        if not lab_config:
            return jsonify({'success': False, 'error': 'No lab configuration found'}), 400
        
        lab_name = assignment_title.lower().replace(' ', '')
        
        # Create zip file with all files
        zip_path = create_submission_zip(student_id, assignment_id, lab_config, lab_name)
        zip_filename = f"{student_name.replace(' ', '_')}_{lab_name}.zip"
        
        # Upload file and attach as comment
        logging.info(f"Uploading {zip_filename} for student {student_id}")
        file_id = upload_file_as_comment(student_id, assignment_id, zip_path, zip_filename)
        
        logging.info(f"Creating submission with comment for student {student_id}")
        submit_to_canvas_with_comment(student_id, assignment_id, file_id, zip_filename)
        
        logging.info(f"Student {student_id} submitted {lab_name} successfully")
        
        return jsonify({
            'success': True,
            'message': f'Submission successful! Your {zip_filename} has been attached to your Canvas submission.'
        })
        
    except Exception as e:
        error_msg = str(e)
        logging.error(f"Submission failed for student {student_id}, assignment {assignment_id}: {error_msg}")
        return jsonify({
            'success': False,
            'error': f'Submission failed: {error_msg}'
        }), 500

@app.errorhandler(413)
def request_entity_too_large(error):
    return jsonify({'error': 'File too large (max 16MB)'}), 413

@app.errorhandler(500)
def internal_error(error):
    logging.error(f"Internal server error: {str(error)}")
    return jsonify({'error': 'Internal server error'}), 500

if __name__ == '__main__':
    app.run(debug=True, host='0.0.0.0', port=5000)
