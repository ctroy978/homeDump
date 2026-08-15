# Handoff: live-test P1 + P2 robustness

**Branch:** `feature/attendance-makeup`  
**Status:** Uncommitted local work. Automated tests: 240 passed, 1 skipped.  
**Out of scope:** P3 (upload size cap, watermark memory, default admin password). Do those later.

This drop is for a classroom live test. One bad student record should not stall the class, and the student form should work without internet.

---

## Before you test

The running server will not see these changes until you restart it.

`data/app.db` is owned by `www-data`. Restart as that user (or whatever already writes the live database) so WAL mode and the new unique-claim index actually apply:

```bash
cd /var/www/homework.local
# stop the old process, then:
uv run main
```

If you start the server as `tcoop` against a `www-data`-owned database, the app still serves pages, but schema updates (unique claim index, WAL) are skipped as read-only. Student claims and attendance writes can then fail or stay on the old locking behavior.

Hard-refresh Chromebooks (`Ctrl+Shift+R`) so they pick up the local HTMX file.

---

## What should feel different

| Area | What to look for |
|------|------------------|
| Student home page | Works with **no internet** on the Chromebook (HTMX is now `/static/htmx.min.js`) |
| Student ID field | Full keyboard, not digits-only |
| Attendance upload | Per-student isolation still holds; banner is **complete / partial / failed**, not always green |
| Upload result | Lists **Qualify for makeup** vs **Do not qualify** codes |
| Admin nav | New **Student lookup** page |
| Print batch | One missing PDF does not block the rest of the class |
| Confirm request | Double-click does not create two queue rows |
| Delete assignment | Works even if that assignment is still in the print queue |
| Student errors | A broken claim shows a calm HTML message, never a raw 500 page |

---

## Live test script

Use a **non-production / throwaway class export** if you can. Real names stay on the server only (FERPA).

### 1. Student form works offline

1. On a Chromebook, turn off internet or stay on the classroom LAN with unpkg blocked.
2. Open the student home page.
3. Choose a period. The Student ID field should appear.
4. If the period dropdown does nothing, the old CDN script is still cached — hard-refresh.

**Pass:** period → ID → Continue works with no CDN.

### 2. Upload a real class file

1. Log in at `/admin/login`.
2. Open **Attendance** (`/admin/attendance`).
3. Upload the usual year-to-date `.txt` (or `.xlsx`) for **one class**.

**Pass:**

- Students with valid SIS import.
- Missing SIS and missing-name-only SIS are listed by name/ID, not silently dropped.
- Banner is **Upload complete**, **Partial import**, or **Upload failed** — matching what actually happened.
- A **Qualify for makeup** / **Do not qualify** code list appears.
- Other classes already in the database are unchanged.

Optional check: upload an `.xlsx` that has a school-name banner above the header. It should still find `Student Name` / `Date` / `Period N`.

### 3. Excused vs unexcused (the product)

1. Open **Student lookup** (`/admin/eligibility`) — also linked from the dashboard.
2. Enter a student ID and period you know:
   - excused day with homework uploaded → “can claim”
   - unexcused day → “not allowable”
   - excused day with **no** assignment yet → “no homework is assigned”
   - ID not in the database → “No student with this ID…”
3. On a Chromebook, have that student request work.

**Pass:**

- Excused + assignment → they see the homework and can confirm.
- Unexcused or no assignment → they see the **generic** student message (not the teacher detail).
- You can explain the generic message from the lookup page without guessing.

### 4. Confirm + print queue

1. Have two qualifying students confirm (or one student confirm twice quickly).
2. Open **Print queue**.
3. Print batch.

**Pass:**

- Double confirm = **one** queue row, not two.
- Batch PDF downloads.
- If you delete an assignment that is still queued, the page does not 500; the queue row for that assignment disappears.

Optional stress: remove one claim PDF from `data/claims/` and print again. Everyone else should still print; the batch cover page names the skipped student; that row stays in the queue.

### 5. Teacher GitHub prep (only if you use it)

Open **GitHub worksheets**. If GitHub is fine, browsing still works. If the token/network is down, you get an error **on the page**, not a 500.

---

## New / changed URLs

| What | URL |
|------|-----|
| Student form | `/` |
| Admin login | `/admin/login` |
| **Student lookup (new)** | `/admin/eligibility` |
| Upload attendance | `/admin/attendance` |
| Print queue | `/admin/print-queue` |
| Local HTMX (sanity) | `/static/htmx.min.js` |

---

## If something fails

| Symptom | Likely cause |
|---------|----------------|
| Period dropdown does nothing | Server not restarted, or Chromebook still has cached unpkg HTMX — hard-refresh |
| Upload works but “database is locked” under load | Server was started as the wrong user; WAL never enabled on `data/app.db` |
| Double confirm created two rows | Same: unique index not applied because DB was read-only at startup |
| Student can claim an unexcused day | Check **Student lookup** and the upload code list; add a missing excused label to `ALLOWABLE_ABSENCE_CODES` only if the SIS label is actually excused |
| `.xls` upload rejected | Expected — use `.txt` or save as `.xlsx` |

Keep notes as: what you uploaded (file type, not student names), what the banner said, and whether the student form worked offline. That is enough to decide if this branch is ready to keep.

---

## After the live test

- P3 is still open on purpose. Do not block go-live on those unless you hit them in class.
- Say what broke or felt wrong; we can patch on this same branch before any merge.
