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
| 3 | Planned | Eligibility engine and tests |
| 4 | Planned | Admin dashboard and assignment uploads |
| 5 | Planned | Student form with HTMX dropdowns |
| 6 | Planned | Claim flow, QR codes, PDF watermarking |
| 7 | Planned | Claim logs, backup script, deployment polish |

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