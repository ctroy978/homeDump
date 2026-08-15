# Plan: Period-tagged attendance import

**Branch:** `feature/period-tagged-import`  
**Goal:** The app knows which students have this teacher in which period, so a 1st-period student cannot claim 3rd-period work.

## Problem

Class exports include Period 0–7 absence columns. Importing a period-1 class currently stores every period. Students pick a period on the form. That is not their schedule.

## Decisions

1. Teacher must choose **which class period this file is** (0–7) on upload.
2. Import **only that period’s column**. Replace **only that period’s** absences for students in the file.
3. Other periods for those students are left alone (another of this teacher’s classes, or leftover rows from old imports).
4. Students in the file become **members** of that period. Students who were members but are missing from this file are marked **inactive** (left the class). Membership rows are kept so they can still request makeup for days they missed while enrolled.
5. Claim / date lookup requires a membership row for the selected period (active or inactive). Leftover period-3 absences from an untagged historical import do **not** grant period-3 work unless the student appeared in a tagged period-3 upload.
6. Present every day in that class: still a member; zero absence rows; nothing to claim. Correct.

## Data

- `attendance_uploads.class_period` — required on new uploads.
- `student_class_periods(student_id, period, last_upload_id, active)` — PK `(student_id, period)`.

## Claim filter

Eligible when: membership for that period exists, allowable absence on that date **in that period**, and an assignment tagged for that period/date.

## Files

Schema/migrations, `attendance_parser.py`, admin attendance route + template, `student_lookup.py`, `claims.py`, `diagnose_claim`, ingest tests, README attendance section.
