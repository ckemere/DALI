"""
Lab Submission System - Complete Application
Includes:
- File upload with template management
- Canvas integration with file attachment
- Compilation queue with position tracking
- Admin dashboard with NetID mapping
"""

from flask import Flask, request, render_template, jsonify, session, redirect, url_for, flash
import os
from werkzeug.utils import secure_filename
import requests
from datetime import datetime
import json
import zipfile
import io
import shutil
import logging
import redis
import uuid
import subprocess
import csv
from concurrent.futures import ThreadPoolExecutor
import threading

# ============================================================================
# FLASK APP SETUP
# ============================================================================

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

# Admin Configuration
ADMIN_PASSWORD = os.environ.get('ADMIN_PASSWORD', 'admin123')

# Lab Configuration
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

# ============================================================================
# COMPILATION QUEUE SYSTEM
# ============================================================================

class CompilationQueue:
    def __init__(self, redis_host='localhost', redis_port=6379, max_workers=16):
        try:
            self.redis = redis.Redis(host=redis_host, port=redis_port, decode_responses=True)
            self.redis.ping()  # Test connection
            logging.info("✓ Redis connected successfully")
        except redis.ConnectionError:
            logging.warning("⚠️  Redis not available - compilation features disabled")
            self.redis = None
        
        self.executor = ThreadPoolExecutor(max_workers=max_workers)
        self.max_workers = max_workers
        
        # Start worker threads only if Redis is available
        if self.redis:
            for _ in range(max_workers):
                self.executor.submit(self._worker_loop)
            logging.info(f"✓ Started {max_workers} compilation workers")
    
    def is_available(self):
        """Check if compilation queue is available"""
        return self.redis is not None
    
    def load_netid_mapping(self, gradebook_csv_path):
        """Load Canvas ID -> NetID mapping from gradebook CSV"""
        if not self.redis:
            return False
        
        try:
            with open(gradebook_csv_path, 'r') as f:
                reader = csv.DictReader(f)
                count = 0
                for row in reader:
                    canvas_id = row.get('ID')
                    netid = row.get('SIS Login ID') or row.get('SIS User ID')
                    
                    if canvas_id and netid:
                        self.redis.hset('netid_map', canvas_id, netid)
                        count += 1
            
            logging.info(f"✓ Loaded {count} netID mappings from {gradebook_csv_path}")
            return True
        except FileNotFoundError:
            logging.warning(f"⚠️  Gradebook not found: {gradebook_csv_path}")
            return False
        except Exception as e:
            logging.error(f"Error loading netID mapping: {e}")
            return False
    
    def get_netid(self, canvas_id):
        """Get netID for a Canvas student ID"""
        if not self.redis:
            return f"canvas_{canvas_id}"
        
        netid = self.redis.hget('netid_map', str(canvas_id))
        return netid or f"canvas_{canvas_id}"
    
    def submit_job(self, student_id, student_name, assignment_id, assignment_name, lab_config, lab_name):
        """Submit a compilation job to the queue"""
        if not self.redis:
            raise Exception("Compilation queue not available (Redis not connected)")
        
        job_id = str(uuid.uuid4())
        netid = self.get_netid(student_id)
        
        job_metadata = {
            'job_id': job_id,
            'student_id': str(student_id),
            'student_name': student_name,
            'netid': netid,
            'assignment_id': str(assignment_id),
            'assignment_name': assignment_name,
            'lab_name': lab_name,
            'status': 'queued',
            'queued_at': datetime.utcnow().isoformat(),
            'started_at': '',
            'completed_at': '',
            'result': ''
        }
        
        # Store metadata
        self.redis.hset(f'job:{job_id}:metadata', mapping=job_metadata)
        
        # Store lab config
        self.redis.set(f'job:{job_id}:config', json.dumps({
            'code_files': lab_config['code_files'],
            'lab_name': lab_name
        }), ex=3600)
        
        # Add to queue
        self.redis.rpush('compile_queue', job_id)
        
        logging.info(f"Queued compilation job {job_id} for student {student_id} ({netid})")
        return job_id
    
    def get_queue_position(self, job_id):
        """Get position of job in queue (1-indexed)"""
        if not self.redis:
            return 0
        
        queue = self.redis.lrange('compile_queue', 0, -1)
        try:
            return queue.index(job_id) + 1
        except ValueError:
            return 0
    
    def get_job_status(self, job_id):
        """Get current status of a job"""
        if not self.redis:
            return None
        
        metadata = self.redis.hgetall(f'job:{job_id}:metadata')
        
        if not metadata:
            return None
        
        # Add queue position if queued
        if metadata['status'] == 'queued':
            metadata['position'] = self.get_queue_position(job_id)
            
            # Estimate wait time
            position = metadata['position']
            avg_compile_time = 5  # seconds
            active_workers = self.redis.scard('compile_active')
            metadata['estimated_wait'] = max(0, int((position - active_workers) * avg_compile_time / self.max_workers))
        
        # Parse result if complete
        if metadata.get('result'):
            try:
                metadata['result'] = json.loads(metadata['result'])
            except:
                pass
        
        return metadata
    
    def cancel_job(self, job_id, student_id):
        """Cancel a queued job"""
        if not self.redis:
            return {'success': False, 'error': 'Queue not available'}
        
        metadata = self.redis.hgetall(f'job:{job_id}:metadata')
        
        if not metadata:
            return {'success': False, 'error': 'Job not found'}
        
        if metadata['student_id'] != str(student_id):
            return {'success': False, 'error': 'Not your job'}
        
        if metadata['status'] != 'queued':
            return {'success': False, 'error': f'Job is {metadata["status"]}, cannot cancel'}
        
        removed = self.redis.lrem('compile_queue', 1, job_id)
        
        if removed:
            self.redis.hset(f'job:{job_id}:metadata', 'status', 'cancelled')
            self.redis.hset(f'job:{job_id}:metadata', 'completed_at', datetime.utcnow().isoformat())
            logging.info(f"Cancelled job {job_id} for student {student_id}")
            return {'success': True, 'message': 'Job cancelled'}
        else:
            return {'success': False, 'error': 'Job already started'}
    
    def get_full_queue(self):
        """Get all queued and active jobs"""
        if not self.redis:
            return []
        
        queued_ids = self.redis.lrange('compile_queue', 0, -1)
        active_ids = list(self.redis.smembers('compile_active'))
        
        jobs = []
        
        for i, job_id in enumerate(queued_ids):
            metadata = self.redis.hgetall(f'job:{job_id}:metadata')
            if metadata:
                metadata['position'] = i + 1
                metadata['state'] = 'queued'
                jobs.append(metadata)
        
        for job_id in active_ids:
            metadata = self.redis.hgetall(f'job:{job_id}:metadata')
            if metadata:
                metadata['position'] = 0
                metadata['state'] = 'compiling'
                jobs.append(metadata)
        
        return jobs
    
    def _worker_loop(self):
        """Worker thread that processes compilation jobs"""
        if not self.redis:
            return
        
        while True:
            try:
                result = self.redis.blpop('compile_queue', timeout=5)
                
                if not result:
                    continue
                
                _, job_id = result
                
                # Mark as active
                self.redis.sadd('compile_active', job_id)
                self.redis.hset(f'job:{job_id}:metadata', 'status', 'compiling')
                self.redis.hset(f'job:{job_id}:metadata', 'started_at', datetime.utcnow().isoformat())
                
                # Get job config
                config_json = self.redis.get(f'job:{job_id}:config')
                if not config_json:
                    self._mark_failed(job_id, 'Job configuration not found')
                    continue
                
                config = json.loads(config_json)
                metadata = self.redis.hgetall(f'job:{job_id}:metadata')
                
                # Run compilation
                result = self._compile(
                    metadata['student_id'],
                    metadata['assignment_id'],
                    config['code_files'],
                    config['lab_name']
                )
                
                # Store result
                self.redis.hset(f'job:{job_id}:metadata', 'result', json.dumps(result))
                self.redis.hset(f'job:{job_id}:metadata', 'status', 'complete' if result['success'] else 'failed')
                self.redis.hset(f'job:{job_id}:metadata', 'completed_at', datetime.utcnow().isoformat())
                
                # Remove from active
                self.redis.srem('compile_active', job_id)
                
                logging.info(f"Completed job {job_id} - {'SUCCESS' if result['success'] else 'FAILED'}")
                
            except Exception as e:
                logging.error(f"Worker error: {e}")
                if 'job_id' in locals():
                    self._mark_failed(job_id, str(e))
    
    def _mark_failed(self, job_id, error_message):
        """Mark job as failed"""
        if not self.redis:
            return
        
        result = {'success': False, 'error': error_message, 'stdout': '', 'stderr': error_message}
        self.redis.hset(f'job:{job_id}:metadata', 'result', json.dumps(result))
        self.redis.hset(f'job:{job_id}:metadata', 'status', 'failed')
        self.redis.hset(f'job:{job_id}:metadata', 'completed_at', datetime.utcnow().isoformat())
        self.redis.srem('compile_active', job_id)
    
    def _compile(self, student_id, assignment_id, code_files, lab_name):
        """Run the compilation"""
        build_dir = get_submission_folder(student_id, assignment_id)
        
        # Ensure all files present
        for filename in code_files:
            student_file = os.path.join(build_dir, filename)
            if not os.path.exists(student_file):
                template_file = get_template_file_path(lab_name, filename)
                if os.path.exists(template_file):
                    shutil.copy(template_file, student_file)
        
        # Create Makefile
        makefile_path = os.path.join(build_dir, 'Makefile')
        if not os.path.exists(makefile_path):
            self._create_makefile(build_dir, code_files)
        
        try:
            result = subprocess.run(
                ['make', 'clean', 'all'],
                cwd=build_dir,
                capture_output=True,
                text=True,
                timeout=30
            )
            
            return {
                'success': result.returncode == 0,
                'stdout': result.stdout,
                'stderr': result.stderr,
                'exit_code': result.returncode
            }
        except subprocess.TimeoutExpired:
            return {
                'success': False,
                'error': 'Compilation timeout (>30 seconds)',
                'stdout': '',
                'stderr': 'Compilation exceeded 30 second limit'
            }
        except Exception as e:
            return {
                'success': False,
                'error': str(e),
                'stdout': '',
                'stderr': str(e)
            }
    
    def _create_makefile(self, build_dir, source_files):
        """Create a simple Makefile"""
        c_files = [f for f in source_files if f.endswith('.c')]
        
        makefile_content = f"""CC = tiarmclang
CFLAGS = -mcpu=cortex-m0plus -mthumb -O2 -g
LDFLAGS = -T linker.lds

SRCS = {' '.join(c_files)}
OBJS = $(SRCS:.c=.o)

all: firmware.elf

firmware.elf: $(OBJS)
\t$(CC) $(CFLAGS) $(LDFLAGS) -o $@ $(OBJS)

%.o: %.c
\t$(CC) $(CFLAGS) -c $< -o $@

clean:
\trm -f *.o firmware.elf

.PHONY: all clean
"""
        
        with open(os.path.join(build_dir, 'Makefile'), 'w') as f:
            f.write(makefile_content)


# Initialize global queue
compile_queue = CompilationQueue(max_workers=16)

# ============================================================================
# HELPER FUNCTIONS
# ============================================================================

def allowed_file(filename, allowed_extensions):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in allowed_extensions

def get_lab_config(assignment_name):
    """Get lab configuration from assignment name"""
    if assignment_name in LAB_CONFIGS:
        return LAB_CONFIGS[assignment_name]
    
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
        # Step 1: Request upload URL
        endpoint = f'courses/{COURSE_ID}/assignments/{assignment_id}/submissions/{student_id}/comments/files'
        data = {
            'name': filename,
            'size': os.path.getsize(file_path),
            'content_type': 'application/zip'
        }
        
        upload_info = canvas_api_request(endpoint, method='POST', data=data)
        upload_url = upload_info['upload_url']
        upload_params = upload_info['upload_params']
        
        # Step 2: Upload file
        with open(file_path, 'rb') as f:
            files = {'file': (filename, f, 'application/zip')}
            upload_response = requests.post(upload_url, data=upload_params, files=files, timeout=60)
            upload_response.raise_for_status()
        
        # Step 3: Confirm upload
        if 'Location' in upload_response.headers:
            confirm_url = upload_response.headers['Location']
            headers = {'Authorization': f'Bearer {CANVAS_API_TOKEN}'}
            confirm_response = requests.get(confirm_url, headers=headers, timeout=30)
            confirm_response.raise_for_status()
            file_data = confirm_response.json()
            file_id = file_data.get('id')
        else:
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
        endpoint = f'courses/{COURSE_ID}/assignments/{assignment_id}/submissions/{student_id}'
        data = {
            'comment': {
                'text_comment': f'Submitted via Lab Submission System. Zip file: {filename}',
                'file_ids': [file_id]
            },
            'submission': {
                'submission_type': 'online_upload',
                'submitted_at': datetime.utcnow().isoformat() + 'Z',
                'posted_grade': '1'  # Nominal grade for visibility
            }
        }
        
        result = canvas_api_request(endpoint, method='PUT', data=data)
        return True
        
    except Exception as e:
        logging.error(f"Submission with comment failed: {str(e)}")
        raise

def create_submission_zip(student_id, assignment_id, lab_config, lab_name):
    """Create a zip file with all required files"""
    submission_folder = get_submission_folder(student_id, assignment_id)
    
    zip_buffer = io.BytesIO()
    
    with zipfile.ZipFile(zip_buffer, 'w', zipfile.ZIP_DEFLATED) as zip_file:
        # Add code files
        for filename in lab_config['code_files']:
            student_file = os.path.join(submission_folder, filename)
            
            if os.path.exists(student_file):
                zip_file.write(student_file, filename)
            else:
                template_file = get_template_file_path(lab_name, filename)
                if os.path.exists(template_file):
                    zip_file.write(template_file, filename)
        
        # Add writeup
        for writeup in lab_config['writeup_files']:
            student_writeup = os.path.join(submission_folder, writeup)
            if os.path.exists(student_writeup):
                zip_file.write(student_writeup, writeup)
                break
    
    # Save to temp file
    temp_zip_path = os.path.join(submission_folder, 'submission.zip')
    with open(temp_zip_path, 'wb') as f:
        f.write(zip_buffer.getvalue())
    
    return temp_zip_path

# ============================================================================
# FLASK ROUTES - MAIN APPLICATION
# ============================================================================

@app.before_first_request
def setup():
    """Initialize on first request"""
    gradebook_path = os.environ.get('GRADEBOOK_CSV_PATH', 'gradebook.csv')
    
    if os.path.exists(gradebook_path) and compile_queue.is_available():
        compile_queue.load_netid_mapping(gradebook_path)

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
        assignment_data = canvas_api_request(f'courses/{COURSE_ID}/assignments/{assignment_id}')
        assignment_title = assignment_data.get('name', 'Assignment')
        
        lab_config = get_lab_config(assignment_title)
        
        if not lab_config:
            flash(f'No lab configuration found for "{assignment_title}". Contact instructor.', 'error')
            return redirect(url_for('home'))
        
        lab_name = assignment_title.lower().replace(' ', '')
        
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
        
        # Check if compilation is available
        compile_available = compile_queue.is_available()
        
        return render_template('assignment_api.html',
                             student_name=student_name,
                             assignment_id=assignment_id,
                             assignment_title=assignment_title,
                             lab_name=lab_name,
                             code_files=lab_config['code_files'],
                             writeup_files=lab_config['writeup_files'],
                             uploaded_files=uploaded_files,
                             compile_available=compile_available)
    
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
    
    is_code = allowed_file(filename, ALLOWED_CODE_EXTENSIONS)
    is_doc = allowed_file(filename, ALLOWED_DOC_EXTENSIONS)
    
    if not (is_code or is_doc):
        return jsonify({'error': 'Invalid file type'}), 400
    
    try:
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
        assignment_data = canvas_api_request(f'courses/{COURSE_ID}/assignments/{assignment_id}')
        assignment_title = assignment_data.get('name', 'Assignment')
        lab_config = get_lab_config(assignment_title)
        
        if not lab_config:
            return jsonify({'success': False, 'error': 'No lab configuration found'}), 400
        
        lab_name = assignment_title.lower().replace(' ', '')
        
        # Create zip file
        zip_path = create_submission_zip(student_id, assignment_id, lab_config, lab_name)
        zip_filename = f"{student_name.replace(' ', '_')}_{lab_name}.zip"
        
        # Upload and attach as comment
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

# ============================================================================
# FLASK ROUTES - COMPILATION SYSTEM
# ============================================================================

@app.route('/compile/<assignment_id>', methods=['POST'])
def start_compile(assignment_id):
    """Submit code for compilation"""
    if 'student_id' not in session:
        return jsonify({'error': 'Not authenticated'}), 403
    
    if not compile_queue.is_available():
        return jsonify({'error': 'Compilation system not available'}), 503
    
    student_id = session['student_id']
    student_name = session['student_name']
    
    try:
        assignment_data = canvas_api_request(f'courses/{COURSE_ID}/assignments/{assignment_id}')
        assignment_title = assignment_data.get('name', 'Assignment')
        lab_config = get_lab_config(assignment_title)
        
        if not lab_config:
            return jsonify({'error': 'No lab configuration found'}), 400
        
        lab_name = assignment_title.lower().replace(' ', '')
        
        # Submit to queue
        job_id = compile_queue.submit_job(
            student_id=student_id,
            student_name=student_name,
            assignment_id=assignment_id,
            assignment_name=assignment_title,
            lab_config=lab_config,
            lab_name=lab_name
        )
        
        status = compile_queue.get_job_status(job_id)
        
        return jsonify({
            'success': True,
            'job_id': job_id,
            'position': status.get('position', 0),
            'estimated_wait': status.get('estimated_wait', 0)
        })
        
    except Exception as e:
        logging.error(f"Failed to queue compilation: {str(e)}")
        return jsonify({'error': str(e)}), 500

@app.route('/compile-status/<job_id>')
def check_compile_status(job_id):
    """Check status of compilation job"""
    status = compile_queue.get_job_status(job_id)
    
    if not status:
        return jsonify({'error': 'Job not found'}), 404
    
    return jsonify(status)

@app.route('/compile-cancel/<job_id>', methods=['POST'])
def cancel_compile(job_id):
    """Cancel a queued compilation job"""
    if 'student_id' not in session:
        return jsonify({'error': 'Not authenticated'}), 403
    
    student_id = session['student_id']
    result = compile_queue.cancel_job(job_id, student_id)
    
    if result['success']:
        return jsonify(result)
    else:
        return jsonify(result), 400

@app.route('/admin/compile-queue')
def admin_compile_queue():
    """Admin dashboard for compilation queue"""
    provided_password = request.args.get('password') or session.get('admin_authenticated')
    
    if provided_password != ADMIN_PASSWORD:
        return render_template('admin_login.html')
    
    session['admin_authenticated'] = ADMIN_PASSWORD
    
    if not compile_queue.is_available():
        return "Compilation system not available (Redis not connected)", 503
    
    jobs = compile_queue.get_full_queue()
    queued_count = len([j for j in jobs if j['state'] == 'queued'])
    compiling_count = len([j for j in jobs if j['state'] == 'compiling'])
    
    return render_template('admin_queue.html',
                         jobs=jobs,
                         queued_count=queued_count,
                         compiling_count=compiling_count,
                         max_workers=compile_queue.max_workers)

@app.route('/admin/compile-queue/data')
def admin_queue_data():
    """Get queue data as JSON for AJAX updates"""
    if session.get('admin_authenticated') != ADMIN_PASSWORD:
        return jsonify({'error': 'Not authenticated'}), 403
    
    if not compile_queue.is_available():
        return jsonify({'error': 'Queue not available'}), 503
    
    jobs = compile_queue.get_full_queue()
    queued_count = len([j for j in jobs if j['state'] == 'queued'])
    compiling_count = len([j for j in jobs if j['state'] == 'compiling'])
    
    return jsonify({
        'jobs': jobs,
        'queued_count': queued_count,
        'compiling_count': compiling_count
    })

# ============================================================================
# ERROR HANDLERS
# ============================================================================

@app.errorhandler(413)
def request_entity_too_large(error):
    return jsonify({'error': 'File too large (max 16MB)'}), 413

@app.errorhandler(500)
def internal_error(error):
    logging.error(f"Internal server error: {str(error)}")
    return jsonify({'error': 'Internal server error'}), 500

# ============================================================================
# MAIN
# ============================================================================

if __name__ == '__main__':
    print("=" * 60)
    print("Lab Submission System Starting")
    print("=" * 60)
    print(f"Canvas API: {CANVAS_BASE_URL}")
    print(f"Course ID: {COURSE_ID}")
    print(f"Compilation: {'ENABLED' if compile_queue.is_available() else 'DISABLED (Redis not available)'}")
    print("=" * 60)
    
    app.run(debug=True, host='0.0.0.0', port=5000)
