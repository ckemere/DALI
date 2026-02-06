"""
Flask Routes for Compilation System
Add these to your app_api_complete.py
"""

from flask import render_template, jsonify, session, request
from compile_queue import compile_queue
import os

# Initialize queue on startup
@app.before_first_request
def setup_compile_queue():
    # Look for gradebook CSV
    gradebook_path = os.environ.get('GRADEBOOK_CSV_PATH', 'gradebook.csv')
    
    if os.path.exists(gradebook_path):
        compile_queue.load_netid_mapping(gradebook_path)
        print(f"✓ Loaded netID mappings from {gradebook_path}")
    else:
        print(f"⚠️  No gradebook found at {gradebook_path}")
        print("   Students will be identified by Canvas ID only")

# Student: Submit compilation job
@app.route('/compile/<assignment_id>', methods=['POST'])
def start_compile(assignment_id):
    """Submit code for compilation"""
    if 'student_id' not in session:
        return jsonify({'error': 'Not authenticated'}), 403
    
    student_id = session['student_id']
    student_name = session['student_name']
    
    try:
        # Get assignment info
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
        
        # Get initial status
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

# Student: Check compilation status
@app.route('/compile-status/<job_id>')
def check_compile_status(job_id):
    """Check status of compilation job"""
    status = compile_queue.get_job_status(job_id)
    
    if not status:
        return jsonify({'error': 'Job not found'}), 404
    
    return jsonify(status)

# Student: Cancel compilation
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

# Admin: View compilation queue
@app.route('/admin/compile-queue')
def admin_compile_queue():
    """Admin dashboard for compilation queue"""
    # Simple auth check - you might want something more robust
    # For now, check if user is instructor (has API token access)
    admin_password = os.environ.get('ADMIN_PASSWORD', 'admin123')
    
    provided_password = request.args.get('password') or session.get('admin_authenticated')
    
    if provided_password != admin_password:
        return render_template('admin_login.html')
    
    # Store in session for convenience
    session['admin_authenticated'] = admin_password
    
    # Get queue data
    jobs = compile_queue.get_full_queue()
    
    # Get stats
    queued_count = len([j for j in jobs if j['state'] == 'queued'])
    compiling_count = len([j for j in jobs if j['state'] == 'compiling'])
    
    return render_template('admin_queue.html',
                         jobs=jobs,
                         queued_count=queued_count,
                         compiling_count=compiling_count,
                         max_workers=compile_queue.max_workers)

# Admin: Get queue data as JSON (for auto-refresh)
@app.route('/admin/compile-queue/data')
def admin_queue_data():
    """Get queue data as JSON for AJAX updates"""
    admin_password = os.environ.get('ADMIN_PASSWORD', 'admin123')
    
    if session.get('admin_authenticated') != admin_password:
        return jsonify({'error': 'Not authenticated'}), 403
    
    jobs = compile_queue.get_full_queue()
    queued_count = len([j for j in jobs if j['state'] == 'queued'])
    compiling_count = len([j for j in jobs if j['state'] == 'compiling'])
    
    return jsonify({
        'jobs': jobs,
        'queued_count': queued_count,
        'compiling_count': compiling_count
    })
