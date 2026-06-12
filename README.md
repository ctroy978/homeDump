# Homework Makeup

A self-contained classroom tool for distributing traceable makeup homework to students who had an allowable absence.

Students on Chromebooks access the app over the local network. The teacher uploads weekly attendance Excel files and homework PDFs. When a student qualifies, they confirm a request; the teacher prints a batch of watermarked PDFs with verification QR codes.

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

## Teacher admin access

The home page is **student-only** — it does not link to teacher tools. Bookmark
these URLs for yourself:

| What | URL |
|------|-----|
| **Admin login** | `http://localhost:8000/admin/login` |
| Dashboard | `http://localhost:8000/admin` |
| Upload attendance | `http://localhost:8000/admin/attendance` |
| Assignments | `http://localhost:8000/admin/assignments` |
| Print queue | `http://localhost:8000/admin/print-queue` |
| Claim logs | `http://localhost:8000/admin/claims` |

On the classroom network, replace `localhost` with your server address (the same
host you set in `PUBLIC_BASE_URL`).

**Password:** the value of `ADMIN_PASSWORD` in your `.env` file (not shown to
students). Default in `.env.example` is `changeme` — change it before go-live.

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

## Verify Phase 6 (print queue)

1. **Prerequisites** — complete the Phase 5 flow until homework appears for a
   qualifying student.

2. **Student request** — click **Confirm request** on an assignment card. The
   student should see a message that homework was submitted for printing (no
   download).

3. **Teacher print queue** — log in and open **Print queue** (`/admin/print-queue`).
   The student's request should appear in the list.

4. **Print batch** — click **Print batch**. A single merged PDF downloads with
   each student's watermarked homework. The queue empties automatically.

5. **Check printed PDF** — confirm:
   - A diagonal text watermark on every page (name, code, period, date)
   - A verification **QR code in the top-right corner of page 1**

6. **Verify QR** — scan the QR code (or open the `/verify/{code}` URL). The page
   should show the registered student, assignment, period, and absence date.

7. **Queue cleanup** — use **Remove** on one row or **Clear queue** to discard
   requests without printing (students can confirm again after removal).

8. **Set `PUBLIC_BASE_URL` in `.env`** so QR codes use the address students
   actually browse to (not `0.0.0.0`):

   ```
   PUBLIC_BASE_URL=http://192.168.1.42:8000
   ```

9. **Run automated tests:**

   ```bash
   uv run pytest tests/test_claims.py tests/test_print_queue.py -v
   ```

## Verify Phase 5 (student form)

1. **Prerequisites** — attendance uploaded (Phase 2) with **Sis Number** in the
   export, and at least one assignment added (Phase 4) for a period/date where a
   test student has an allowable absence.

2. **Start the server:**

   ```bash
   uv run main
   ```

3. **Open the home page** at `http://localhost:8000/`

4. **Walk through the student flow:**
   - **Period** — only periods with uploaded assignments appear
   - **Student ID** — enter the student's SIS number (not a name dropdown)
   - **Date** — only that student's eligible absence dates appear
   - **Homework** — matching assignments (confirm for printing in Phase 6)

5. **Run automated tests:**

   ```bash
   uv run pytest tests/test_student_lookup.py -v
   ```

6. **HTMX partials** — each step loads via:
   - `/student/sis-field?period=N`
   - `POST /student/lookup` with `period` and `sis_number`
   - `POST /student/assignments` with `period`, `sis_number`, and `date`

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

   Expected tables: `assignments`, `attendance_records`, `attendance_uploads`, `claim_logs`, `claim_tokens`, `print_queue`, `students`

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
| `PUBLIC_BASE_URL` | **Required for go-live** — classroom IP or hostname for student links and QR codes |
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
| 5 | **Done** | Student form with SIS lookup and HTMX dropdowns |
| 6 | **Done** | Print queue, watermarked PDFs, QR verification |
| 7 | **Done** | Claim log review, backup/restore, admin download |

**All planned phases complete.** Say if you want further polish or deployment help.

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

## Verify Phase 7 (claim logs and backup)

1. **Claim logs** — log in at `/admin/login`, open **Claim logs**, and confirm
   student claim attempts appear with success/failure status. Filter by student
   name or result. Successful codes link to `/verify/{token}`.

2. **Download backup** — on the admin dashboard, click **Download backup**
   and save the `.tar.gz` file on your computer or cloud storage.

3. **Backup to USB** — or mount your drive and run:

   ```bash
   uv run python scripts/backup_data.py /run/media/$USER/YOUR-USB-NAME
   ```

   The script writes `homedump-data-YYYYMMDD-HHMMSS.tar.gz` to the drive.
   Typical classroom data is small (database + PDFs) — a USB stick is plenty.

4. **Restore** — stop the server first, then run (with your saved file):

   ```bash
   uv run python scripts/restore_data.py /path/to/homedump-data-....tar.gz --yes
   uv run main
   ```

   Your previous `data/` folder is moved to `data.before-restore-...` automatically.

5. **Run automated tests:**

   ```bash
   uv run pytest tests/test_claim_logs.py tests/test_data_backup.py -v
   ```

## Backup (overview)

Back up the `data/` directory regularly. Use `scripts/backup_data.py` to write a
compressed archive to a USB drive or other folder. Archives contain classroom
data only — not `.env` secrets. Use `scripts/restore_data.py` to recover.

## Common commands

```bash
uv sync                                          # Install / update dependencies
uv run main                                      # Start the classroom server
uv add <package>                                 # Add a dependency
uv add --dev pytest                              # Add a dev dependency (Phase 3+)
uv run pytest                                    # Run tests (Phase 3+)
uv run python scripts/backup_data.py /path/to/usb
uv run python scripts/restore_data.py /path/to/backup.tar.gz --yes
```