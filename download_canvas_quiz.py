#!/usr/bin/env python3
"""
Download Canvas quiz submissions to CSV.

Usage:
    python download_canvas_quiz.py --canvas-url <url> --token <token> --course-id <id> --quiz-id <id> --output <file>

Environment variables:
    CANVAS_URL       Canvas instance URL
    CANVAS_API_TOKEN Canvas API token
    COURSE_ID        Course ID
    CANVAS_QUIZ_ID   Quiz ID

Example with CLI args:
    python download_canvas_quiz.py --canvas-url https://canvas.instructure.com --token your_api_token --course-id 12345 --quiz-id 67890 --output lab8_answers.csv

Example with env vars:
    export CANVAS_URL=https://canvas.instructure.com
    export CANVAS_API_TOKEN=your_api_token
    export COURSE_ID=12345
    export CANVAS_QUIZ_ID=67890
    python download_canvas_quiz.py --output lab8_answers.csv
"""

import argparse
import csv
import os
import requests
from typing import List, Dict, Any
from urllib.parse import urljoin
from tqdm import tqdm


class CanvasQuizDownloader:
    def __init__(self, canvas_url: str, api_token: str):
        self.base_url = canvas_url.rstrip('/')
        self.api_token = api_token
        self.session = requests.Session()
        self.session.headers.update({'Authorization': f'Bearer {api_token}'})

    def get_quiz_submissions(self, course_id: int, quiz_id: int) -> List[Dict[str, Any]]:
        """Fetch all submissions for a quiz."""
        url = urljoin(self.base_url, f'/api/v1/courses/{course_id}/quizzes/{quiz_id}/submissions')
        submissions = []
        page = 1

        pbar = tqdm(desc='Fetching submissions', unit=' pages', position=0)
        while True:
            params = {'page': page, 'per_page': 100}
            resp = self.session.get(url, params=params)
            resp.raise_for_status()

            data = resp.json()
            # Quiz submissions API wraps data in {'quiz_submissions': [...]}
            quiz_submissions = data.get('quiz_submissions', [])

            if not quiz_submissions:
                break

            submissions.extend(quiz_submissions)
            pbar.update(1)
            pbar.set_postfix({'total': len(submissions)})
            page += 1

        pbar.close()
        return submissions

    def get_user(self, user_id: int) -> Dict[str, Any]:
        """Fetch user info by ID."""
        url = urljoin(self.base_url, f'/api/v1/users/{user_id}')
        resp = self.session.get(url)
        resp.raise_for_status()
        return resp.json()

    def get_quiz_submission_questions(self, quiz_submission_id: int) -> List[Dict[str, Any]]:
        """Fetch questions and answers for a specific quiz submission."""
        url = urljoin(self.base_url, f'/api/v1/quiz_submissions/{quiz_submission_id}/questions')
        resp = self.session.get(url)
        resp.raise_for_status()
        data = resp.json()
        # Response is wrapped in {'quiz_submission_questions': [...]}
        return data.get('quiz_submission_questions', [])

    def download_to_csv(self, course_id: int, quiz_id: int, output_file: str):
        """Download quiz submissions and save to CSV."""
        print(f"Fetching quiz submissions...")
        submissions = self.get_quiz_submissions(course_id, quiz_id)

        if not submissions:
            print("No submissions found.")
            return

        # Keep only the latest attempt per student
        latest_by_user = {}
        for submission in submissions:
            user_id = submission.get('user_id')
            attempt = submission.get('attempt', 0)

            if user_id not in latest_by_user or attempt > latest_by_user[user_id]['attempt']:
                latest_by_user[user_id] = submission

        latest_submissions = list(latest_by_user.values())

        # Flatten data for CSV
        rows = []
        for submission in tqdm(latest_submissions, desc='Processing submissions', unit=' submissions'):
            submission_id = submission.get('id')
            user_id = submission.get('user_id')

            # Fetch user info
            try:
                user_info = self.get_user(user_id)
                user_name = user_info.get('name')
                user_email = user_info.get('login_id')
            except Exception as e:
                print(f"Warning: Could not fetch user info for user {user_id}: {e}")
                user_name = f"User {user_id}"
                user_email = ""

            # Fetch submission questions and answers
            try:
                questions = self.get_quiz_submission_questions(submission_id)
            except Exception as e:
                print(f"Warning: Could not fetch questions for submission {submission_id}: {e}")
                questions = []

            base_row = {
                'Student ID': user_id,
                'Student Name': user_name,
                'Email': user_email,
                'Score': submission.get('score'),
                'Finished At': submission.get('finished_at'),
                'Attempt': submission.get('attempt'),
            }

            # Add answers for each question
            for question in questions:
                q_id = question.get('id')
                q_text = question.get('question_text', f'Question {q_id}')
                answer = question.get('user_answer', '')
                base_row[f'Q{q_id}: {q_text[:50]}'] = answer

            rows.append(base_row)

        # Get all unique columns
        fieldnames = ['Student ID', 'Student Name', 'Email', 'Score', 'Finished At', 'Attempt']
        for row in rows:
            for key in row:
                if key not in fieldnames:
                    fieldnames.append(key)

        # Write CSV
        print(f"Writing to {output_file}...")
        with open(output_file, 'w', newline='', encoding='utf-8') as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(rows)

        print(f"✓ Downloaded {len(latest_submissions)} latest attempts to {output_file}")


def main():
    parser = argparse.ArgumentParser(description='Download Canvas quiz submissions to CSV')
    parser.add_argument('--canvas-url', default=os.getenv('CANVAS_URL'), help='Canvas instance URL (env: CANVAS_URL)')
    parser.add_argument('--token', default=os.getenv('CANVAS_API_TOKEN'), help='Canvas API token (env: CANVAS_API_TOKEN)')
    parser.add_argument('--course-id', type=int, default=os.getenv('COURSE_ID'), help='Course ID (env: COURSE_ID)')
    parser.add_argument('--quiz-id', type=int, default=os.getenv('CANVAS_QUIZ_ID'), help='Quiz ID (env: CANVAS_QUIZ_ID)')
    parser.add_argument('--output', default='quiz_answers.csv', help='Output CSV file (default: quiz_answers.csv)')

    args = parser.parse_args()

    if not args.canvas_url:
        parser.error('--canvas-url required (or set CANVAS_URL)')
    if not args.token:
        parser.error('--token required (or set CANVAS_API_TOKEN)')
    if not args.course_id:
        parser.error('--course-id required (or set COURSE_ID)')
    if not args.quiz_id:
        parser.error('--quiz-id required (or set CANVAS_QUIZ_ID)')

    try:
        downloader = CanvasQuizDownloader(args.canvas_url, args.token)
        downloader.download_to_csv(args.course_id, args.quiz_id, args.output)
    except requests.exceptions.HTTPError as e:
        print(f"❌ API Error: {e}")
        print(f"   Status Code: {e.response.status_code}")
        print(f"   Response: {e.response.text}")
    except Exception as e:
        import traceback
        print(f"❌ Error: {e}")
        traceback.print_exc()


if __name__ == '__main__':
    main()
