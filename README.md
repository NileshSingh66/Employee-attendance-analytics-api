# Employee Attendance & Analytics API

A REST API built with **Python, FastAPI, and MongoDB** for employee management, attendance tracking, and attendance analytics.

## Features

- Employee records and department-based filtering
- Attendance punch-in and punch-out
- Attendance listing with date filters and pagination
- Attendance status regularization
- Monthly employee attendance analytics
- Department attendance summaries and trends
- Late-arrival leaderboard with ranking
- MongoDB indexes and query explain endpoints

## Tech Stack

- Python
- FastAPI
- MongoDB
- PyMongo

## Prerequisites

- Python 3.10 or newer
- MongoDB 6.0 or newer

## Local Setup

1. Clone this repository and open its folder in a terminal.
2. Create and activate a virtual environment:

   ```powershell
   python -m venv .venv
   .venv\Scripts\Activate.ps1
   ```

3. Install dependencies:

   ```powershell
   pip install -r requirements.txt
   ```

4. Configure the MongoDB connection using a local `.env` file with:

   ```text
   MONGO_URI=mongodb://127.0.0.1:27017
   MONGO_DB=attendance_db
   ```

5. Start MongoDB and load the sample data if required:

   ```powershell
   python sample_seed.py
   ```

6. Start the API:

   ```powershell
   python -m uvicorn app.main:app --port 8000 --reload
   ```

7. Open the interactive API documentation:

   http://127.0.0.1:8000/docs

## Project Structure

```text
app/
  main.py
requirements.txt
REVIEW.md
DECISIONS.md
README.md
```

## Submission

This project is submitted as part of the Software Engineer Trainee assignment. See `REVIEW.md` for the starter-code review and `DECISIONS.md` for implementation decisions.

**Security:** Do not commit `.env`, credentials, or other secrets to the public repository.

