#!/usr/bin/env python3
"""
Canvas Gradebook Parser with Password Generator
Parses a Canvas gradebook CSV and generates secure passwords for each student.
"""

import csv
import secrets
import string
from pathlib import Path


def generate_secure_password(length=12):
    """
    Generate a cryptographically secure random password.
    
    Password includes:
    - At least one uppercase letter
    - At least one lowercase letter
    - At least one digit
    - At least one special character
    
    Args:
        length: Password length (default: 12)
    
    Returns:
        A secure random password string
    """
    # Define character sets
    uppercase = string.ascii_uppercase
    lowercase = string.ascii_lowercase
    digits = string.digits
    special = "!@#$%&*-_=+"
    
    # Ensure at least one character from each set
    password = [
        secrets.choice(uppercase),
        secrets.choice(lowercase),
        secrets.choice(digits),
        secrets.choice(special)
    ]
    
    # Fill the rest with random characters from all sets
    all_chars = uppercase + lowercase + digits + special
    password += [secrets.choice(all_chars) for _ in range(length - 4)]
    
    # Shuffle to avoid predictable pattern
    secrets.SystemRandom().shuffle(password)
    
    return ''.join(password)


def parse_gradebook(input_file, output_file):
    """
    Parse Canvas gradebook and create simplified CSV with passwords.
    
    Args:
        input_file: Path to Canvas gradebook CSV
        output_file: Path for output CSV
    """
    students = []
    
    with open(input_file, 'r', encoding='utf-8') as f:
        reader = csv.DictReader(f)
        
        for row in reader:
            # Skip the "Points Possible" row and any empty rows
            student_name = row.get('Student', '').strip()
            
            if not student_name or 'Points Possible' in student_name:
                continue
            
            # Extract required fields
            netid = row.get('SIS Login ID', '').strip()
            canvas_id = row.get('ID', '').strip()
            
            # Skip if missing critical information
            if not netid or not canvas_id:
                continue
            
            # Generate secure password
            password = generate_secure_password()
            
            students.append({
                'netid': netid,
                'name': student_name,
                'canvas_id': canvas_id,
                'password': password
            })
    
    # Write to output CSV
    with open(output_file, 'w', newline='', encoding='utf-8') as f:
        fieldnames = ['netid', 'name', 'canvas_id', 'password']
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        
        writer.writeheader()
        writer.writerows(students)
    
    return len(students)


def main():
    """Main function to run the script."""
    # File paths
    input_file = 'GRADEBOOK.csv' # CHANGE THIS
    output_file = 'student_passwords.csv'
    
    # Ensure output directory exists
    Path(output_file).parent.mkdir(parents=True, exist_ok=True)
    
    # Process the gradebook
    print(f"Processing gradebook: {input_file}")
    num_students = parse_gradebook(input_file, output_file)
    
    print(f"\nSuccess! Processed {num_students} students.")
    print(f"Output file: {output_file}")
    print("\nThe output CSV contains:")
    print("  - netid: Student's network ID")
    print("  - name: Student's full name")
    print("  - canvas_id: Canvas student number")
    print("  - password: Secure randomly generated password (12 characters)")
    print("\nPassword format: Mix of uppercase, lowercase, digits, and special characters")


if __name__ == '__main__':
    main()
