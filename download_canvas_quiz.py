#!/usr/bin/env python3
"""
Download Canvas quiz submissions to CSV.

Usage:
    python download_canvas_quiz.py --canvas-url <url> --token <token> --course-id <id> --quiz-id <id> --output <file>

Example:
    python download_canvas_quiz.py --canvas-url https://canvas.instructure.com --token your_api_token --course-id 12345 --quiz-id 67890 --output lab8_answers.csv
"""

import argparse
import csv
import requests
from typing import List, Dict, Any
from urllib.parse import urljoin


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

        while True:
            params = {'page': page, 'per_page': 100}
            resp = self.session.get(url, params=params)
            resp.raise_for_status()

            data = resp.json()
            if not data:
                break

            submissions.extend(data)
            page += 1

        return submissions

    def get_quiz_questions(self, course_id: int, quiz_id: int) -> List[Dict[str, Any]]:
        """Fetch quiz questions to map question IDs to text."""
        url = urljoin(self.base_url, f'/api/v1/courses/{course_id}/quizzes/{quiz_id}/questions')
        questions = []
        page = 1

        while True:
            params = {'page': page, 'per_page': 100}
            resp = self.session.get(url, params=params)
            resp.raise_for_status()

            data = resp.json()
            if not data:
                break

            questions.extend(data)
            page += 1

        return questions

    def download_to_csv(self, course_id: int, quiz_id: int, output_file: str):
        """Download quiz submissions and save to CSV."""
        print(f"Fetching quiz questions...")
        questions = self.get_quiz_questions(course_id, quiz_id)
        question_map = {q['id']: q['question_text'] for q in questions}

        print(f"Fetching quiz submissions...")
        submissions = self.get_quiz_submissions(course_id, quiz_id)

        if not submissions:
            print("No submissions found.")
            return

        # Flatten data for CSV
        rows = []
        for submission in submissions:
            user = submission.get('user', {})
            base_row = {
                'Student ID': user.get('id'),
                'Student Name': user.get('display_name'),
                'Email': user.get('login_id'),
                'Score': submission.get('score'),
                'Finished At': submission.get('finished_at'),
                'Attempt': submission.get('attempt'),
            }

            # Add answers for each question
            for answer in submission.get('submission_data', []):
                q_id = answer.get('question_id')
                question_text = question_map.get(q_id, f'Question {q_id}')
                base_row[f'Q{q_id}: {question_text[:50]}'] = answer.get('text')

            rows.append(base_row)

        # Get all unique columns
        fieldnames = ['Student ID', 'Student Name', 'Email', 'Score', 'Finished At', 'Attempt']
        for row in rows:
            for key in row:
                if key not in fieldnames:
                    fieldnames.append(key)

        # Write CSV
        with open(output_file, 'w', newline='', encoding='utf-8') as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(rows)

        print(f"✓ Downloaded {len(submissions)} submissions to {output_file}")


def main():
    parser = argparse.ArgumentParser(description='Download Canvas quiz submissions to CSV')
    parser.add_argument('--canvas-url', required=True, help='Canvas instance URL (e.g., https://canvas.instructure.com)')
    parser.add_argument('--token', required=True, help='Canvas API token')
    parser.add_argument('--course-id', type=int, required=True, help='Course ID')
    parser.add_argument('--quiz-id', type=int, required=True, help='Quiz ID')
    parser.add_argument('--output', default='quiz_answers.csv', help='Output CSV file (default: quiz_answers.csv)')

    args = parser.parse_args()

    try:
        downloader = CanvasQuizDownloader(args.canvas_url, args.token)
        downloader.download_to_csv(args.course_id, args.quiz_id, args.output)
    except requests.exceptions.HTTPError as e:
        print(f"❌ API Error: {e}")
        print(f"   Response: {e.response.text}")
    except Exception as e:
        print(f"❌ Error: {e}")


if __name__ == '__main__':
    main()
