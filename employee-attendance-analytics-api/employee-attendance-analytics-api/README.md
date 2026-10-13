# Employee Attendance & Analytics API

A backend API built with **Python, FastAPI, and MongoDB** for managing employee attendance. It supports employee records, punch-in/punch-out, attendance corrections with an audit history, and attendance analytics using MongoDB aggregation pipelines.

> **Project status:** This repository has been syntax-checked, but it has not been fully integration-tested against a live MongoDB instance. Run the checks below with your own database before using it as a demonstration or submission.

## What this project does

- **Manage employees** — create employees and retrieve a paginated employee list.
- **Record attendance** — punch in and punch out for an employee.
- **Validate attendance rules** — reject unknown employees, duplicate daily punch-ins, invalid timestamps, and invalid punch-out requests.
- **Correct attendance safely** — update an existing attendance record with a reason, the identifier of the person making the correction, and a history of changed fields.
- **Generate analytics** — return monthly employee summaries, department summaries, a late-arrival leaderboard, and department attendance trends.
- **Inspect query plans** — expose an admin endpoint that returns MongoDB explain output for selected query patterns.

## Technology stack

- **Python** — application logic and attendance rules
- **FastAPI** — HTTP API and interactive API documentation
- **PyMongo** — Python client for MongoDB
- **MongoDB Atlas or local MongoDB** — data storage and aggregation pipelines
- **Pydantic** — request validation

## How the application works

1. A client sends an HTTP request to an API endpoint.
2. FastAPI validates the request body and parameters.
3. Python applies the attendance business rules.
4. PyMongo reads or writes documents in MongoDB.
5. The API returns a JSON response to the client.

For example, a punch-in request is checked against the employee collection. If the employee exists and no attendance record already exists for that employee and attendance date, the application saves the punch-in. MongoDB's unique indexes help enforce uniqueness even when two requests arrive close together.

## Repository layout

```text
employee-attendance-analytics-api/
├── app/
│   ├── __init__.py
│   └── main.py              # FastAPI application and endpoint logic
├── sample_data/
│   ├── employees.json       # Example employee documents
│   └── attendance_logs.json # Example attendance documents
├── .env.example             # Environment-variable template (no real credentials)
├── .gitignore
├── DATA_MODEL.md             # Document shapes and data conventions
├── DECISIONS.md              # Design choices and reasoning
├── REVIEW.md                 # Existing-code review and fixes
├── openapi.yaml              # API contract
├── requirements.txt          # Python dependencies
└── sample_seed.py            # Optional sample-data loader
```

## Requirements

- Python 3.10 or newer (Python 3.11+ recommended)
- MongoDB Atlas cluster or a local MongoDB server
- Internet access to install Python dependencies

The department trend endpoint uses MongoDB aggregation features including `$documents`, `$dateAdd`, and `$setWindowFields`. Use **MongoDB 6.0 or newer** for that endpoint.

## Run locally on Windows

### 1. Open the project folder

Open this folder in VS Code. Open **Terminal → New Terminal** and make sure the terminal is in the project root—the folder containing `requirements.txt` and `app/`.

### 2. Create and activate a virtual environment

```powershell
py -m venv .venv
.\.venv\Scripts\Activate.ps1
```

If PowerShell blocks activation, you can use Command Prompt and run:

```bat
.venv\Scripts\activate.bat
```

### 3. Install dependencies

```powershell
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
```

### 4. Configure MongoDB

Copy the example environment file:

```powershell
Copy-Item .env.example .env
```

Open `.env` and set your connection details:

```env
MONGO_URI=mongodb+srv://YOUR_USERNAME:YOUR_PASSWORD@YOUR_CLUSTER_HOST/?appName=Cluster0
MONGO_DB=attendance_db
```

Replace the example values with your own MongoDB Atlas values. In Atlas, make sure the database user exists and your current IP address is allowed under **Network Access**. If the password contains reserved URL characters, URL-encode them in the connection string.

**Security:** Never commit `.env`, passwords, API keys, or a real database connection string to GitHub. `.env` is listed in `.gitignore`; only commit `.env.example`.

For a local MongoDB installation, you can instead use:

```env
MONGO_URI=mongodb://localhost:27017
MONGO_DB=attendance_db
```

### 5. Start the API

```powershell
uvicorn app.main:app --port 8000 --reload
```

Keep this terminal open while using the API. On startup, the application attempts to create its database indexes. If startup fails, check the terminal traceback and verify the MongoDB connection settings.

### 6. Open the API documentation

Visit these addresses in your browser:

- Interactive API documentation: `http://127.0.0.1:8000/docs`
- Alternative documentation: `http://127.0.0.1:8000/redoc`
- Health check: `http://127.0.0.1:8000/health`

In Swagger UI, expand an endpoint, select **Try it out**, enter the required values, and select **Execute**. Start with `GET /health` before testing other endpoints.

## API endpoints

| Method | Endpoint | Purpose |
|---|---|---|
| `GET` | `/health` | Check whether MongoDB is reachable |
| `POST` | `/employees` | Create an employee |
| `GET` | `/employees` | List employees; supports department filtering and pagination |
| `POST` | `/attendance/punch-in` | Record an employee's punch-in |
| `POST` | `/attendance/punch-out` | Record an employee's punch-out |
| `GET` | `/attendance` | List attendance with optional employee, date, status, and pagination filters |
| `PATCH` | `/attendance/{emp_code}/{date}` | Correct an existing attendance record and append audit history |
| `GET` | `/analytics/employees/{emp_code}/monthly` | Get an employee's monthly attendance summary |
| `GET` | `/analytics/departments/summary` | Get department-level monthly attendance summaries |
| `GET` | `/analytics/leaderboard/late` | Rank employees by total late minutes for a month |
| `GET` | `/analytics/departments/{department}/trend` | Get daily department attendance trends over a date range |
| `GET` | `/admin/explain/{endpoint}` | Inspect MongoDB query execution details for supported operations |

For parameter requirements and response schemas, use `/docs` or refer to `openapi.yaml`.

## Example: create an employee

In Swagger UI, expand `POST /employees` and use **Try it out**. A request body has this shape:

```json
{
  "emp_code": "EMP0001",
  "name": "Rahul Sharma",
  "email": "rahul@example.com",
  "department": "Engineering",
  "shift_start": "09:30",
  "shift_end": "18:30",
  "joined_on": "2026-01-15"
}
```

This is example data only. The employee code must match the format `EMP` followed by 4–6 digits. `shift_start` and `shift_end` use 24-hour `HH:MM` format. `joined_on` uses `YYYY-MM-DD` format.

After creating the employee, use `POST /attendance/punch-in` and `POST /attendance/punch-out` with that employee code. Check the request schemas in Swagger UI before executing them.

### Timestamp format

Where a request accepts `punched_at`, `punch_in`, or `punch_out`, provide an **integer Unix epoch timestamp in milliseconds**. Leave `punched_at` out of a punch request to use the server's current time. The API stores instants as UTC MongoDB dates and returns punch timestamps as epoch milliseconds.

## Attendance rules implemented

- Employee codes must be unique.
- One attendance document is allowed per employee per attendance date.
- A punch-in requires an existing employee.
- A second punch-in for the same attendance date is rejected.
- A punch-out requires an existing employee and a prior punch-in.
- Punch-out must occur after punch-in and within 24 hours.
- Attendance corrections require a reason and a `regularized_by` identifier.
- Corrections append a history entry with the change time, actor, reason, and changed fields.
- Attendance calculations use the employee's configured shift and India Standard Time (IST) rules.

These are the rules encoded in the current implementation. Review the assignment requirements and test edge cases before submission.

## Optional: load sample data

The repository includes a sample-data loader:

```powershell
python sample_seed.py
```

**Warning: this script deletes all documents in the `employees` and `attendance_logs` collections in the configured database before inserting the included sample documents.** Use a dedicated development/test database and run it only if you are comfortable replacing those collections' contents. Do not run it against a database containing data you need to keep.

## Testing checklist

Before demonstrating or submitting the project, test at least these cases through `/docs`:

- `GET /health` succeeds when MongoDB is reachable.
- Creating an employee succeeds; reusing the same employee code returns a conflict.
- Punch-in succeeds for a known employee.
- A duplicate punch-in is rejected.
- Punch-out succeeds after punch-in.
- Punch-out without a prior punch-in is rejected.
- Attendance can be filtered by employee, date range, and status.
- A correction requires a reason and actor and adds an audit-history entry.
- Analytics endpoints return sensible results for populated and empty date ranges.
- Invalid dates, months, timestamps, and pagination values are rejected.

## Design and code review notes

- `DATA_MODEL.md` describes the stored documents and timestamp conventions.
- `DECISIONS.md` explains the main design decisions, indexing strategy, and scaling considerations.
- `REVIEW.md` records the starter-code issues reviewed and the corresponding fixes.

## Troubleshooting

**The application fails during startup**

- Read the full traceback in the terminal.
- Check that `.env` is in the project root and uses `MONGO_URI` and `MONGO_DB`.
- Verify the Atlas username/password, cluster hostname, and Network Access allowlist.
- Confirm that the virtual environment is active and dependencies installed successfully.

**Swagger says “Failed to fetch”**

- Confirm the server is still running in the terminal.
- Open `http://127.0.0.1:8000/health` directly.
- If the health endpoint returns an error, inspect the terminal and MongoDB connection settings.

**A request returns `404`, `409`, or `422`**

These are commonly used HTTP status codes: `404` means the requested employee or attendance record was not found; `409` indicates a conflict such as a duplicate employee or attendance record; `422` means request data or business-rule validation failed. Read the response body for the specific explanation.

## License

No license has been specified. Add a license file if you intend to permit reuse or redistribution.
