"""
Compilation Queue System
- Queue position tracking
- Admin dashboard
- Job cancellation
- NetID mapping
"""

import redis
import json
import uuid
import subprocess
import time
import csv
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor
import threading

class CompilationQueue:
    def __init__(self, redis_host='localhost', redis_port=6379, max_workers=16):
        self.redis = redis.Redis(host=redis_host, port=redis_port, decode_responses=True)
        self.executor = ThreadPoolExecutor(max_workers=max_workers)
        self.max_workers = max_workers
        
        # Start worker threads
        for _ in range(max_workers):
            self.executor.submit(self._worker_loop)
    
    def load_netid_mapping(self, gradebook_csv_path):
        """Load Canvas ID -> NetID mapping from gradebook CSV"""
        try:
            with open(gradebook_csv_path, 'r') as f:
                reader = csv.DictReader(f)
                for row in reader:
                    # Canvas gradebook typically has:
                    # "ID", "SIS User ID", "SIS Login ID", etc.
                    canvas_id = row.get('ID')
                    netid = row.get('SIS Login ID') or row.get('SIS User ID')
                    
                    if canvas_id and netid:
                        self.redis.hset('netid_map', canvas_id, netid)
            
            print(f"Loaded {self.redis.hlen('netid_map')} netID mappings")
            return True
        except Exception as e:
            print(f"Error loading netID mapping: {e}")
            return False
    
    def get_netid(self, canvas_id):
        """Get netID for a Canvas student ID"""
        netid = self.redis.hget('netid_map', str(canvas_id))
        return netid or f"canvas_{canvas_id}"
    
    def submit_job(self, student_id, student_name, assignment_id, assignment_name, lab_config, lab_name):
        """Submit a compilation job to the queue"""
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
            'started_at': None,
            'completed_at': None,
            'result': None
        }
        
        # Store metadata
        self.redis.hmset(f'job:{job_id}:metadata', job_metadata)
        
        # Store lab config (for worker to use)
        self.redis.set(f'job:{job_id}:config', json.dumps({
            'code_files': lab_config['code_files'],
            'lab_name': lab_name
        }), ex=3600)
        
        # Add to queue
        self.redis.rpush('compile_queue', job_id)
        
        return job_id
    
    def get_queue_position(self, job_id):
        """Get position of job in queue (1-indexed)"""
        queue = self.redis.lrange('compile_queue', 0, -1)
        try:
            position = queue.index(job_id) + 1
            return position
        except ValueError:
            # Not in queue - either running or complete
            return 0
    
    def get_job_status(self, job_id):
        """Get current status of a job"""
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
            metadata['estimated_wait'] = max(0, (position - active_workers) * avg_compile_time / self.max_workers)
        
        # Parse result if complete
        if metadata['result']:
            metadata['result'] = json.loads(metadata['result'])
        
        return metadata
    
    def cancel_job(self, job_id, student_id):
        """Cancel a queued job (only if owned by student)"""
        metadata = self.redis.hgetall(f'job:{job_id}:metadata')
        
        if not metadata:
            return {'success': False, 'error': 'Job not found'}
        
        # Verify ownership
        if metadata['student_id'] != str(student_id):
            return {'success': False, 'error': 'Not your job'}
        
        # Can only cancel if queued
        if metadata['status'] != 'queued':
            return {'success': False, 'error': f'Job is {metadata["status"]}, cannot cancel'}
        
        # Remove from queue
        removed = self.redis.lrem('compile_queue', 1, job_id)
        
        if removed:
            # Update status
            self.redis.hset(f'job:{job_id}:metadata', 'status', 'cancelled')
            self.redis.hset(f'job:{job_id}:metadata', 'completed_at', datetime.utcnow().isoformat())
            return {'success': True, 'message': 'Job cancelled'}
        else:
            return {'success': False, 'error': 'Job already started'}
    
    def get_full_queue(self):
        """Get all queued and active jobs (for admin dashboard)"""
        queued_ids = self.redis.lrange('compile_queue', 0, -1)
        active_ids = list(self.redis.smembers('compile_active'))
        
        jobs = []
        
        # Queued jobs
        for i, job_id in enumerate(queued_ids):
            metadata = self.redis.hgetall(f'job:{job_id}:metadata')
            if metadata:
                metadata['position'] = i + 1
                metadata['state'] = 'queued'
                jobs.append(metadata)
        
        # Active jobs
        for job_id in active_ids:
            metadata = self.redis.hgetall(f'job:{job_id}:metadata')
            if metadata:
                metadata['position'] = 0
                metadata['state'] = 'compiling'
                jobs.append(metadata)
        
        return jobs
    
    def _worker_loop(self):
        """Worker thread that processes compilation jobs"""
        while True:
            try:
                # Block until job available (5 second timeout)
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
                
            except Exception as e:
                print(f"Worker error: {e}")
                if 'job_id' in locals():
                    self._mark_failed(job_id, str(e))
    
    def _mark_failed(self, job_id, error_message):
        """Mark job as failed"""
        result = {'success': False, 'error': error_message}
        self.redis.hset(f'job:{job_id}:metadata', 'result', json.dumps(result))
        self.redis.hset(f'job:{job_id}:metadata', 'status', 'failed')
        self.redis.hset(f'job:{job_id}:metadata', 'completed_at', datetime.utcnow().isoformat())
        self.redis.srem('compile_active', job_id)
    
    def _compile(self, student_id, assignment_id, code_files, lab_name):
        """Actually run the compilation"""
        from app_api_complete import get_submission_folder, get_template_file_path
        import os
        import shutil
        
        build_dir = get_submission_folder(student_id, assignment_id)
        
        # Ensure all files present (student + templates)
        for filename in code_files:
            student_file = os.path.join(build_dir, filename)
            if not os.path.exists(student_file):
                template_file = get_template_file_path(lab_name, filename)
                if os.path.exists(template_file):
                    shutil.copy(template_file, student_file)
        
        # Create Makefile if needed
        makefile_path = os.path.join(build_dir, 'Makefile')
        if not os.path.exists(makefile_path):
            self._create_makefile(build_dir, code_files)
        
        try:
            # Run compilation
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


# Global queue instance
compile_queue = None

def init_compile_queue(gradebook_csv_path=None):
    """Initialize the compilation queue"""
    global compile_queue
    compile_queue = CompilationQueue(max_workers=16)
    
    if gradebook_csv_path:
        compile_queue.load_netid_mapping(gradebook_csv_path)
    
    return compile_queue
