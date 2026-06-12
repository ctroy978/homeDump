# Homework Makeup

A self-contained classroom tool for distributing traceable makeup homework to students who had an allowable absence.

Students on Chromebooks access the app over the local network. The teacher uploads weekly attendance Excel files and homework PDFs. When a student qualifies, the app generates a unique code, QR code, and watermarked PDF.

## Requirements

- [uv](https://docs.astral.sh/uv/) for Python dependency management
- Python 3.11+ (uv will install one if needed)

## Quick start (Phase 1)

```bash
cd /path/to/homeDump
cp .env.example .env
uv sync
uv run main
```

Open in a browser:

- Home page: `http://localhost:8000/`
- Health check: `http://localhost:8000/health`

On the classroom network, students use `http://<server-ip>:8000` instead of `localhost`.

## Attendance import model

Exports are year-to-date, but you download them **one class at a time**. Imports
are **per student**, not per class:

1. The app finds every student in the file (by **SIS number** when the export
   includes it, otherwise by name).
2. For each student, it **deletes all of their attendance** and reloads
   year-to-date rows from that file.
3. Students who are not in the file are left unchanged.

This lets you upload Period 3, then Period 5, without wiping other classes.
Re-upload whenever late excused notes arrive.

**Schedule changes (Period 3 → Period 5):** once a student moves, they disappear
from the Period 3 export and show up in Period 5. Upload the **Period 5** report
to refresh them — the year-to-date export should include their full history,
including older Period 3 absences with any updated codes. Their old snapshot
stays put until they appear in a new upload, so always refresh from the class
export that currently contains them.

## Verify Phase 2 (attendance upload)

1. **Build a test fixture** from the anonymized sample (adds a fake student name):

   ```bash
   uv run python scripts/build_test_fixture.py
   ```

2. **Start the server** (if not already running):

   ```bash
   uv run main
   ```

3. **Upload via browser** — open `http://localhost:8000/admin/attendance` and upload either:
   - `tests/fixtures/named_attendance.txt` (tab-delimited, like real exports)
   - `tests/fixtures/named_attendance.xlsx`

   Or upload via curl:

   ```bash
   curl -F "file=@tests/fixtures/named_attendance.txt" http://localhost:8000/admin/attendance/upload -L
   ```

   **Real live reports:** upload your `.txt` export on the server only. Never commit it or share it in chat (FERPA).

4. **Verify database contents:**

   ```bash
   sqlite3 data/app.db "SELECT COUNT(*) FROM students;"
   sqlite3 data/app.db "SELECT COUNT(*) FROM attendance_records;"
   sqlite3 data/app.db "SELECT period, absence_code, COUNT(*) FROM attendance_records GROUP BY 1,2 ORDER BY 3 DESC LIMIT 10;"
   ```

   Expected: 1 student (`Test Student A`), hundreds of attendance records, and sparse period mapping (e.g. `Unexcused Absence` in period 3 only for 2025-09-02).

5. **Verify sparse period parsing** — the first row of the sample has a code only in Period 3:

   ```bash
   sqlite3 data/app.db "SELECT absence_date, period, absence_code FROM attendance_records WHERE absence_date='2025-09-02';"
   ```

   Expected: one row — `2025-09-02|3|Unexcused Absence`

## Verify Phase 5 (student form)

1. **Prerequisites** — attendance uploaded (Phase 2) and at least one assignment
   added (Phase 4) for a period/date where a test student has an allowable absence.

2. **Start the server:**

   ```bash
   uv run main
   ```

3. **Open the home page** at `http://localhost:8000/`

4. **Walk through the cascading dropdowns:**
   - **Period** — only periods with uploaded assignments appear
   - **Name** — students with an allowable absence on a date that has homework
   - **Date** — eligible absence dates for that student and period
   - **Homework** — matching assignments (download comes in Phase 6)

5. **Run automated tests:**

   ```bash
   uv run pytest tests/test_student_lookup.py -v
   ```

6. **HTMX partials** — each dropdown loads via:
   - `/student/names?period=N`
   - `/student/dates?period=N&student=...`
   - `/student/assignments?period=N&student=...&date=YYYY-MM-DD`

## Verify Phase 4 (admin + assignments)

1. **Set a real admin password** in `.env`:

   ```
   ADMIN_PASSWORD=your-strong-password
   SECRET_KEY=some-long-random-string
   ```

2. **Restart the server:**

   ```bash
   uv run main
   ```

3. **Log in** at `http://localhost:8000/admin/login`

4. **Add an assignment** — Period, assigned date, title, and a PDF file.

5. **Verify files on disk:**

   ```bash
   sqlite3 data/app.db "SELECT id, period, assigned_date, title FROM assignments;"
   ls -la data/assignments/*/original.pdf
   ```

6. **Confirm attendance upload requires login** — visiting `/admin/attendance` without logging in should redirect to the login page.

## Verify Phase 3 (eligibility)

1. **Run automated tests:**

   ```bash
   uv add --dev pytest   # first time only
   uv run pytest tests/test_eligibility.py -v
   ```

2. **Optional live check** — set `DEBUG=true` in `.env`, restart, then:

   ```bash
   curl "http://localhost:8000/dev/eligibility?student=STUDENT_NAME&period=3&date=2025-09-02"
   ```

   Replace `STUDENT_NAME` and the date/period with values from your uploaded attendance data. Use this only on the server — do not paste real student names into chat.

3. **Customize allowable codes** in `.env`:

   ```
   ALLOWABLE_ABSENCE_CODES=Excused Absence,Sports-Athletics,Illness,...
   ```

## Verify Phase 1

1. **Health endpoint** — should return `"status": "ok"` and list six tables:

   ```bash
   curl http://localhost:8000/health
   ```

2. **Database tables** — confirm all schema tables exist:

   ```bash
   sqlite3 data/app.db ".tables"
   ```

   Expected tables: `assignments`, `attendance_records`, `attendance_uploads`, `claim_logs`, `claim_tokens`, `students`

3. **Restart test** — stop and restart the server. It should start cleanly with no errors (schema init is idempotent).

## Project layout

```
app/           Python application code
templates/     Jinja2 HTML templates
static/        CSS and static assets
data/          SQLite database, uploads, PDFs (not committed to git)
tests/         Automated tests (added in later phases)
scripts/       Utility scripts (added in later phases)
```

## Configuration

Copy `.env.example` to `.env` and edit as needed:

| Variable | Purpose |
|----------|---------|
| `ADMIN_PASSWORD` | Teacher admin login (Phase 4+) |
| `SECRET_KEY` | Signs admin session cookies |
| `HOST` / `PORT` | Server bind address |
| `DATA_DIR` | Where database and uploaded files live |
| `ALLOWABLE_ABSENCE_CODES` | Comma-separated absence codes that qualify |
| `DEBUG` | Enable developer-only routes when `true` |

## Development phases

| Phase | Status | What it adds |
|-------|--------|--------------|
| 1 | **Done** | Project foundation, database schema, health check |
| 2 | **Done** | Attendance Excel upload and parsing |
| 3 | **Done** | Eligibility engine and tests |
| 4 | **Done** | Password-protected admin and assignment uploads |
| 5 | **Done** | Student form with HTMX dropdowns |
| 6 | **Current** | Claim flow, QR codes, PDF watermarking |
| 7 | Planned | Claim logs, backup script, deployment polish |

**Current focus: Phase 6** — claim flow with unique codes, QR codes, and watermarked PDF downloads.

After testing each phase, say **"build Phase N"** to continue.

## Student data and FERPA

**Never commit or share real attendance exports.** They contain student names and other protected information.

- Real `.txt`, `.xlsx`, `.csv` exports stay on the classroom server only
- All common export formats are listed in `.gitignore`
- Do not paste student names or attendance rows into chat, tickets, or git
- The anonymized `cleanatt.xlsx` sample (no names) is for development only

### Safe testing workflow

1. **Development** — use `cleanatt.xlsx` and the named fixture from `scripts/build_test_fixture.py`
2. **Real-world validation** — upload your live report on the server via `/admin/attendance` and verify locally:

   ```bash
   sqlite3 data/app.db "SELECT COUNT(*) FROM students;"
   sqlite3 data/app.db "SELECT period, absence_code, COUNT(*) FROM attendance_records GROUP BY 1,2 LIMIT 10;"
   ```

3. **If parser debugging is needed** — run checks on the server yourself. Share only non-PII details (column headers, delimiter, error messages) if you need help.

### Sample files (anonymized only)

- `cleanatt.xlsx` — anonymized sample (no student names)
- `toprow.xlsx` — shows the full export column headers

Run `uv run python scripts/build_test_fixture.py` to build a named test fixture from `cleanatt.xlsx`.

## Backup (overview)

Back up the entire project folder periodically, especially the `data/` directory which holds the database, attendance uploads, and assignment PDFs. A backup script will be added in Phase 7.

## Common commands

```bash
uv sync                                          # Install / update dependencies
uv run main                                      # Start the classroom server
uv add <package>                                 # Add a dependency
uv add --dev pytest                              # Add a dev dependency (Phase 3+)
uv run pytest                                    # Run tests (Phase 3+)
```