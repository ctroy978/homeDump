# Plan: Robust SIS-identity attendance import

**Branch:** `robust-sis-identity-import`  
**Status:** Implemented on branch `robust-sis-identity-import`  
**Goal:** One bad or incomplete student must not block the rest of a class upload; student ID (SIS) is the only unique identity; names may collide.

---

## Locked decisions (from product discussion)

| # | Decision |
|---|----------|
| 1 | Rows/students **without an SIS are rejected**, with a **clear teacher-facing message** so the missing ID can be fixed in the source system. |
| 2 | **No migration project.** Treat the classroom DB as fluid: students move periods/classes constantly; each upload refreshes whoever appears in that file. |
| 3 | **Duplicate SIS in one file** → industry default: **one person**, profile fields **last write wins**, attendance rows for the same `(date, period)` **last write wins** (already close to current parse behavior). |
| 4 | SIS: **no fixed length**; **no decimals** (reject or strip is not needed for `.0` Excel floats per operator assurance); normalize with **trim only** (and reject blank after trim). Do not invent padding/leading-zero rules. |
| 5 | **Name is never unique.** Two students may share a display name; only SIS decides identity. |
| 6 | **Per-student import isolation:** success for student A is durable even if student B fails later in the same file. |

---

## Problem recap

Today:

- `students.name` is `UNIQUE` → two “Jordan Lee”s (or name clash with an existing row) can **fail the entire upload**.
- Import runs in **one transaction** → one integrity error rolls back everyone in that file.
- Student UI already keys on SIS; eligibility still joins on **name**, which is unsafe once names can collide.
- Missing SIS can still create name-only students who **cannot** use the claim form.

---

## Target identity model

```text
SIS number  = unique person key (required for import + claim)
Name        = display label only (duplicates allowed; updates on re-upload)
Grade       = optional attribute (updates when present)
Not in file = leave that person untouched (schedule moves are fluid)
```

**Schedule / class moves (unchanged product rule, reaffirmed):**

- Upload is still **one class export at a time**.
- When a student **appears** in a file (by SIS), replace **all** of their attendance from that file’s YTD snapshot.
- When a student **does not appear**, leave them alone (they may still be in another period’s last upload).
- Moving Period 3 → Period 5: upload the **new** class export; same SIS refreshes history.

Nothing about “cohort wipe of whole class” — only per-SIS replace.

---

## Proposed work packages

### WP1 — Schema: SIS unique, name not unique

**Files:** `app/database.py` (and any tests assuming unique names)

- `students.name`: keep `NOT NULL`, **remove `UNIQUE`**.
- `students.sis_number`: treat as required for app-managed students:
  - Prefer schema that enforces uniqueness for non-null SIS (already have partial unique index).
  - App layer: never insert a student without SIS on the attendance path.
- Fresh installs / `init_schema`: define the desired shape directly.
- **Migration:** per product direction, **no careful upgrade path**. Acceptable approaches (pick one at implement time):
  - Document “wipe `data/app.db` / restore from next upload” for this classroom tool, **or**
  - Lightweight recreate of `students` table on startup only if the old unique-name constraint is detected (optional convenience — not a formal migration product).

**Out of scope:** rewriting historical claim_logs (they store `student_name` as a string snapshot — fine).

---

### WP2 — SIS normalization rules (minimal)

**Files:** `app/services/attendance_parser.py`, `app/services/student_lookup.py` (shared helper preferred)

| Input | Result |
|-------|--------|
| whitespace only / empty | reject (missing SIS) |
| leading/trailing spaces | strip |
| contains `.` (decimal) | **reject** with message (guardrail; operator said real IDs won’t be floats, but cheap safety) |
| any non-empty string after strip, no `.` | accept as-is (variable length; keep leading zeros if present) |

Do **not**: force numeric-only unless we learn IDs are always digits; do **not** pad length; do **not** strip internal spaces unless we later see real exports do that.

Lookup uses the **same** normalize function as import so teacher and student see consistent IDs.

---

### WP3 — Import: reject no-SIS; SID-only identity; per-student commit

**Files:** `app/services/attendance_parser.py`, `app/routers/admin.py`, attendance templates

#### 3a. Parse / roster

- Student identity key = **SIS only** (after normalize).
- Rows with valid name + date + codes but **no SIS** → do not create a student; count as reject or row skip with reason **“Missing student ID (SIS number)”**, including **student name** when available so you can chase the ID.
- Rows with SIS but bad/missing date → skip that row (existing); student may still load other rows.
- File-level failures unchanged: no header, wrong extension, unreadable file → hard fail whole upload.

#### 3b. Upsert

```text
if SIS exists in DB:
  update name, grade (when provided)
  replace attendance for that student_id from this file
else:
  INSERT new student (sis_number, name, grade)
  insert attendance from this file
```

- **Remove** name-based match / `ON CONFLICT(name)` paths from the attendance import path.
- Duplicate SIS in file: one roster entry (last name/grade wins); attendance dict already last-wins per `(student, date, period)`.

#### 3c. Isolation

For each SIS in the file:

1. `BEGIN` or rely on autocommit boundaries / explicit `commit` per student  
2. try: upsert + replace attendance  
3. on success: commit; increment success counters  
4. on failure: rollback that student only; append to `rejected[]` with name, SIS, reason  
5. next student  

Upload metadata row (`attendance_uploads`): create once at start (or after first success — prefer once at start so rejects still tie to an upload attempt). Successful students get `last_attendance_upload_id` set.

#### 3d. Result object + teacher UI

Extend `AttendanceParseResult` (or equivalent) with:

- `students_succeeded`
- `students_rejected` (list of `{name?, sis?, reason}`)
- keep `records_upserted`, `rows_skipped`, `records_cleared`

Admin success page after upload must show:

- N students updated, M records saved  
- **Rejected list** (e.g. “Alex Rivera — Missing student ID (SIS number). Add their SIS in the attendance export and re-upload.”)  
- Optionally skipped-row count  

Whole-file exceptions still show as a single error (structure/encoding).

---

### WP4 — Eligibility and claims by student id / SIS (not name)

**Files:** `app/services/eligibility.py`, `app/services/student_lookup.py`, `app/services/claims.py`, tests

- Change `check_eligibility` to take **`student_id`** (preferred) or SIS, and query:

  `WHERE ar.student_id = ? AND ar.period = ? AND ar.absence_date = ?`

- Keep `student_name` on the result for display only (loaded from the student row).
- Call sites that currently pass `student.name` must pass id after SIS resolution.
- Claims already resolve SIS → student; no change to the student-facing form UX.

This avoids “wrong Jordan Lee” if two share a name.

---

### WP5 — Tests

**Files:** `tests/test_attendance_ingest.py`, `tests/test_eligibility.py`, `tests/test_student_lookup.py`, `tests/conftest.py` as needed

Must-have cases:

1. Two students, **same name**, different SIS → both import; both claimable by own SIS.  
2. Missing SIS row with a name → rejected with message containing name + missing ID guidance; **other students still commit**.  
3. Integrity/processing error on one student (if injectable) → others succeed.  
4. Same SIS twice in one file, different names → one student, last name wins; attendance merged last-wins.  
5. Student moves: appear in period 3 file then period 5 file → same SIS, attendance refreshed (existing test adapted to require SIS).  
6. Eligibility with two same-name students → only the correct SIS’s absences count.  
7. Name-only upsert path removed or not used by import; fixtures always supply SIS.  
8. SIS with leading/trailing spaces normalizes; SIS containing `.` rejected.  

Regression: class A upload does not wipe class B; empty period cells still sparse.

---

### WP6 — Docs / admin copy

**Files:** `README.md` (attendance section), `templates/admin/attendance.html`

- State clearly: **Sis Number column required**; missing ID students are listed and skipped.  
- Names may duplicate.  
- Re-upload the class that currently contains the student after schedule changes.  
- Point teachers at the reject list after upload.

---

## Explicit non-goals (this branch)

- Fuzzy matching absence codes  
- Excel float SIS coercion beyond reject-if-decimal  
- Formal multi-version DB migrations for production fleets  
- Changing claim/print/watermark UX beyond identity correctness  
- Hardening “clear to zero attendance when all dates bad” (optional follow-up; call out in risks)

---

## Risks and open points for discussion

| Risk | Notes | Proposal |
|------|--------|----------|
| Operator deletes DB | No migration → re-upload class files | Acceptable for classroom tool |
| All-bad-dates for one SIS still “succeeds” with 0 rows after clear | Can wipe prior eligibility silently | **Optional WP7:** if student had prior records and this file yields 0 parseable rows for them, **reject refresh** and keep old data + message. Prefer yes for robustness; confirm. |
| Reject list long | 30 missing SISs | Show all or first 20 + “and N more”; full list always in result object for tests |
| Numeric-only SIS? | Unknown | Accept any non-empty string without `.` until real export says otherwise |
| Existing name-only rows in old local DB | Won’t match new rules | Wipe/reimport; not supported |

---

## Implementation order (when approved)

1. Schema + shared `normalize_sis_number`  
2. Upsert/import by SIS only + reject missing SIS  
3. Per-student commit + result rejections  
4. Admin UI messaging  
5. Eligibility by `student_id`  
6. Tests  
7. README / template copy  
8. (Optional) refuse clear-to-empty on re-upload  

Estimated shape: focused change set in parser, schema, eligibility, admin attendance page, tests — not a rewrite of claims/distribution.

---

## Success criteria

- Upload a file with 100 students where 1 has no SIS and 2 share a name → **99 in DB** (or 100 if only the no-SIS fails), **reject message names the missing-SIS student**, both same-name students exist under different SISs.  
- One student’s failure never rolls back another student’s successful import in the same file.  
- Student claim still uses SIS; duplicate names do not cross-wire eligibility.  
- Schedule move via new class export still refreshes the same SIS.  
- `uv run pytest` green for attendance, eligibility, student lookup, and related route tests.

---

## Approval checklist

Before coding, confirm:

- [ ] WP7 (refuse clear-to-empty) in or out of this branch  
- [ ] Reject decimal SIS (`.`) as planned  
- [ ] Accept non-digit characters in SIS if present, or digits-only?  
- [ ] DB wipe / recreate OK (no migration)  
- [ ] Anything else out of scope  

Once approved, implement on `robust-sis-identity-import` in the order above.
